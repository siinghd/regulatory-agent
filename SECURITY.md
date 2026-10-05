# Security policy

## Report a vulnerability

Send an email to **security@hsingh.app**. Include this information:

- What you found.
- Where you found it: the address, the URL, the request id or the message id.
- The steps to reproduce it.
- The impact that you think it has.
- How we can contact you.

To send an encrypted report, ask for it in your first message. We then agree on a method with you.

Do not open a public issue for a security problem.

What to expect:

| Step | Time |
|---|---|
| Acknowledgement | Not more than 3 business days |
| First assessment (Is the report valid? What is the severity?) | Not more than 10 business days |
| Fix | Critical: 7 days. High: 30 days. Medium: 90 days ([policy](docs/policies/vulnerability-management.md)). |
| Disclosure | We agree on the disclosure with you. We give you credit, unless you tell us not to. |

We do not pay a bug bounty.

## Scope

In scope:

- The email agent at **agent@hsingh.app**. This includes sender authentication, the detection of loops and auto-replies, and how the agent reads requests. It also includes prompt injection, and what the agent sends to which person.
- The viewer at **https://uarb.hsingh.app**: the citation pages, the progress pages, the file downloads, the status page (`/status`) and the privacy page (`/privacy`).
- The read-only Grafana view at **https://uarb.hsingh.app/grafana/**: our configuration of it. An example is a page or an API that must stay closed.
- The download links that the agent sends: how the agent uses the links, their expiry and their encryption.
- The code and the deployment configuration in this repository.

Out of scope:

- Denial of service, volumetric tests, load tests and spam.
- Social engineering, phishing and physical attacks.
- Third-party services and other `*.hsingh.app` sites, unless the problem has a direct effect on the agent. Third-party services include Cloudflare, OpenRouter and its model providers, TypeSafe, the regulator portals and the hosting providers.
- Reports from automatic scanners without a demonstrated impact.
- Missing headers or best practices without a real attack.
- Answers from the agent to mail that you send from your own correctly authenticated address. This is the function of the agent.

## Rules of engagement

- Use only email addresses and domains that you control.
- Do not spoof the addresses or the domains of other persons.
- Do not make the agent send mail to a person other than you.
- Send only a small number of requests each hour. The agent accepts not more than 6 requests from each sender each hour.
- If you see the data of other persons, stop immediately and tell us.
- Do not access, change or delete data that is not yours. Do not keep data that you find.
- Do not try to make the service worse for other users.

## Safe harbour

If you act in good faith and obey this policy, we consider your research as authorised. In that case:

- We will not start or support legal action against you. This includes action under anti-hacking laws or anti-circumvention laws.
- We will not report you to the authorities.
- A third party can start legal action against you for research that obeyed this policy. In that case, we will make it known that you acted with our authorisation.

If you are not sure that an action is in scope, or that the rules above accept it, ask at security@hsingh.app before you do it.

The machine-readable contact is at `https://uarb.hsingh.app/.well-known/security.txt`.
