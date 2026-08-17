# Pull request

## Summary

<!-- Explain the problem and the chosen solution. -->

## Security and privacy

<!--
Describe effects on credentials, browser sessions, visited or permitted URLs,
page data, downloads, networking, logs, or durable state. Write "None" when
there are none.
-->

- [ ] I used only public test pages and sanitized fixtures.
- [ ] I reviewed the complete diff for secrets and personal data.
- [ ] I preserved the documented security invariants or explained an intentional change.

## Validation

<!-- List the exact commands and relevant manual checks that passed. -->

- [ ] Tests cover the changed behavior and important failure paths.
- [ ] `make check` passes.
- [ ] `make docker-check` passes, or the change does not affect Docker,
      packaging, dependencies, entrypoints, or container security.
- [ ] Documentation is updated, or no documentation change is needed.
