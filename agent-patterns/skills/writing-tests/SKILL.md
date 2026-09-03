---
name: writing-tests
description: (no description)
disable-model-invocation: true
---

- in R, use the `testthat` testing framework
- plan unit tests before writing any new functionality or bug fixes
  - your tests should cover the desired new outcome, and should fail at first
  - write tests for corner cases and exceptions in additioon to the happy path
  - aim for >80% code coverage
  - run tests and iterate your functionality until the tests pass
