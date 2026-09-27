#!/usr/bin/env bash
# Path:     test/run_all_tests.sh
# Project:  smtp-alert-dispatcher
# Revision: 1
# Updated:  2026-09-26
# Purpose:  The single test entry point for this repository. CI runs exactly
#           this command, and so can you, from any directory:
#
#               bash test/run_all_tests.sh;
#
#           Exits 0 only when every suite below passed. Each suite reports
#           how many tests it executed, and a suite that executed none fails
#           the run, here and in CI alike. When TESTS_EXECUTED_FILE is set, as
#           CI sets it, each suite also appends "<label><TAB><count>" to that
#           file. Scratch output goes to one temporary directory, removed on
#           exit.
#
# Suites:
#           unittest discovery over test/, counted from its own summary line.
#           Negative control: every defect test/inject_defect.py knows must fail the suite.
set -euo pipefail;

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)";
cd "${project_root}";
work_directory="$(mktemp -d "${TMPDIR:-/tmp}/revisualized_tests.XXXXXX")";
trap 'rm -rf -- "${work_directory}"' EXIT;

fail() {
  echo "FAIL: ${1}" >&2;
  exit 1;
};

# Refuses a count of zero or a count that is not a number. A suite that ran
# nothing has not passed.
record_tests_executed() {
  local label="${1}";
  local count="${2}";
  if ! [[ "${count}" =~ ^[0-9]+$ ]] || [ "${count}" -eq 0 ]; then
    fail "suite '${label}' reported '${count}' tests executed";
  fi;
  echo "  executed: ${label}: ${count}";
  if [ -n "${TESTS_EXECUTED_FILE:-}" ]; then
    printf '%s\t%s\n' "${label}" "${count}" >> "${TESTS_EXECUTED_FILE}";
  fi;
};

# The house runner. The count comes from unittest's own "Ran N tests" line.
# Python 3.12 and later also exit 5 when discovery finds nothing; 3.9 exits
# 0, which is why the count is checked rather than trusted.
run_unittest_suite() {
  local log_file="${work_directory}/unittest.log";
  local tests_ran;
  echo "== unittest: python3 -m unittest discover -s test -v ($(python3 --version 2>&1))";
  python3 -m unittest discover -s test -v 2>&1 | tee "${log_file}";
  tests_ran="$(sed -n 's/^Ran \([0-9][0-9]*\) tests\{0,1\} in .*/\1/p' "${log_file}" | tail -1)";
  record_tests_executed "unittest discover -s test" "${tests_ran:-0}";
};

# Negative control. Each known defect is injected, one at a time, into a
# scratch copy of the module and its tests, and the suite must fail against
# it. The working tree is never modified, so an interrupted run cannot leave
# a defect behind.
run_defect_injection() {
  local injection_root="${work_directory}/injection";
  local defect_names=();
  local defect_name;
  local status;
  echo "== negative control: the suite must reject each injected defect";
  mapfile -t defect_names < <(python3 test/inject_defect.py --list);
  [ "${#defect_names[@]}" -gt 0 ] || fail "test/inject_defect.py --list named no defects";
  for defect_name in "${defect_names[@]}"; do
    rm -rf -- "${injection_root}";
    mkdir -p "${injection_root}";
    cp -p smtp_alert_dispatcher.py "${injection_root}/";
    cp -Rp test "${injection_root}/test";
    python3 "${injection_root}/test/inject_defect.py" "${defect_name}" > /dev/null;
    status=0;
    (cd "${injection_root}" && python3 -m unittest discover -s test) > "${work_directory}/injection.log" 2>&1 || status="${?}";
    if [ "${status}" -eq 0 ]; then
      cat "${work_directory}/injection.log" >&2;
      fail "the suite passed with '${defect_name}' injected; that regression is not gated";
    fi;
    echo "  ok: suite rejected '${defect_name}' (exit ${status})";
  done;
  record_tests_executed "injected defects rejected" "${#defect_names[@]}";
};

run_unittest_suite;
run_defect_injection;

echo "All suites passed.";
