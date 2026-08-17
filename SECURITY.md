# Security Policy

`browser-use-mcp` can interact with authenticated browser sessions, private page
content, downloads, and external systems. Report suspected vulnerabilities
privately and use public test pages or sanitized fixtures in reports.

## Deployment security model

The MCP caller is authorized to control browser sessions and any named profile
in its configured tenant. Page content, navigation targets, redirects,
subresources, selectors, model responses, and downloads are untrusted. Steel,
the browser egress proxy or firewall, target websites, and the model provider
are separate operational trust boundaries.

An Internet-reachable deployment requires all of the following:

- HTTPS termination at a trusted reverse proxy, with the application port kept
  private and `BROWSER_USE_MCP_TLS_TERMINATED=true`;
- bearer authentication for service-to-service use, or an OAuth-capable MCP
  gateway for public end-user authorization;
- an authoritative public-only Steel egress boundary before setting
  `BROWSER_USE_MCP_PUBLIC_NETWORK_EGRESS_ENFORCED=true`;
- exact MCP hosts, browser origins, and model provider origins;
- request and connection limits at the reverse proxy in addition to the
  process-level browser, timeout, body, and response limits; and
- access-controlled collection and alerting for `browser_use_mcp.audit` logs.

The CDP request guard checks initial requests, redirects, subresources, frames,
and workers before HTTP request bytes are sent. Chrome can establish a TCP
connection before that pause. The guard is defense in depth: local DNS
resolution in the MCP process cannot replace browser-side egress enforcement
against private destination reachability, split-horizon DNS, or rebinding.

Semantic tools disclose Stagehand-generated page context to the configured
model endpoint. Depending on the operation, that can include page text,
DOM-derived structure, form values, and images or screenshots. Security reports
must not include those payloads. Named browser profiles remain in Steel after a
session closes and must be covered by the deployment's retention and deletion
process.

## Supported versions

Security fixes are currently provided for the `main` branch. After the first
release, fixes will also be provided for the latest published release. Older
releases should be upgraded before a report is considered resolved.

| Version | Supported |
| --- | --- |
| Current `main` branch | Yes |
| Published releases | None yet |
| Older releases | No |

## Reporting a vulnerability

Use
[GitHub private vulnerability reporting](https://github.com/s-block/browser-use-mcp/security/advisories/new).
If the private form is unavailable, open a
[private-contact request](https://github.com/s-block/browser-use-mcp/issues/new?template=private_contact.yml)
containing no vulnerability details or sensitive data.
Do not open a public issue containing exploitable details, credentials, cookies,
browser profiles, proxy URLs, private URLs, form values, downloads, or private
page content.

Include:

- the affected version or commit;
- a minimal reproduction using a public test page or sanitized fixture;
- the expected and observed behavior;
- the security impact and required preconditions; and
- any suggested remediation or disclosure deadline.

We aim to acknowledge a complete report within three business days, provide an
initial assessment within seven business days, and coordinate disclosure after
a fix is available. These are response targets rather than contractual service
levels.

## Research guidelines

Please:

- test only systems, accounts, and browser profiles you own or are explicitly
  authorized to use;
- do not access, enumerate, modify, or disrupt third-party systems or data;
- do not perform denial-of-service testing against public or third-party
  services;
- stop if testing exposes another person's credentials, session, or private
  data;
- retain only the minimum evidence needed for the report; and
- allow a reasonable remediation period before public disclosure.

Good-faith research that follows this policy will not be treated as malicious
activity by this project. This statement cannot authorize testing of websites,
browser providers, MCP hosts, model providers, or any other third-party system.
