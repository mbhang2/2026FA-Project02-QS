---
name: writing-tests
description: R-first test-writing basics — use testthat, plan tests before writing functionality (they should fail first), cover corner cases and exceptions, aim for >80% coverage. User-invoked study aid; the build loop's tester follows the test-driven-development skill instead.
disable-model-invocation: true
---

- in R, use the `testthat` testing framework
- plan unit tests before writing any new functionality or bug fixes
  - your tests should cover the desired new outcome, and should fail at first
  - write tests for corner cases and exceptions in addition to the happy path
  - aim for >80% code coverage
  - run tests and iterate your functionality until the tests pass
