# Privacy notice: the regulatory document agent

This document is written in ASD-STE100 Simplified Technical English.

*Last updated 5 October 2026.* The page https://uarb.hsingh.app/privacy shows the same notice. The page takes its retention periods from the configuration of the agent.

This notice tells what happens to your information when you send an email to **agent@hsingh.app**. It also tells what happens when you open a link from the agent. The operator of the service, H. Singh ("we"), runs it. Send questions or requests to **privacy@hsingh.app**.

## What the agent does

You send it a request, for example "send me the Other Documents for M12205". The agent reads your email and gets the public filings from the website of the regulator. It then replies with the documents, a short summary, and links to the exact passages that the summary uses.

## What we collect

- **Your email:** your address and display name, the subject and the text of your message, and its technical headers. The headers include the IP address of the server that sent the email to us. They also include the results of the checks that prove that the email came from you. The agent reads only the text of your message. It does not open attachments. They stay inside the stored copy of the email.
- **What we did with it:** the request that we understood, its progress, and the emails that we sent to you.
- **When you open our links:** our web server and our network provider record standard access logs. These logs contain your IP address, your browser, the page and the time.

We do not use cookies to track you. We do not make profiles. We do not sell or rent your information.

## Why we use it

- To answer your request and send the documents to you. This is why you wrote to us.
- To keep the service safe. We answer only email that we can prove comes from its sender. We also limit how often a person can use the service. For this reason, nobody can use the service to send mail to people who did not ask for it.
- To keep the service in operation and accountable. We use the data to find the cause of failures and to keep a record of what the system did.

## Who else processes it

- **TypeSafe** (`api.typesafe.ai`) runs a classification model, Jev, for us.
  - When our rules cannot understand an email, we send its subject and text to TypeSafe. TypeSafe then classifies the request.
  - To check the citations of a summary, we send short excerpts of the public documents to TypeSafe, with the summary sentences that cite them.
  - TypeSafe does not train on the data that we send. But on our plan, it is **not zero data retention**. TypeSafe can keep what we send for a period under its own terms.
- **AI language models, through OpenRouter.** When TypeSafe cannot classify an email confidently, or when TypeSafe is not available, we send the text of the email to a language model. A language model also writes the summaries from the public documents. OpenRouter sends our requests only to model providers that do not keep or train on the data.
- We never send your attachments, the data of other people or our passwords to TypeSafe or to a language model.
- **Hosting:** **Hetzner** hosts our server in Helsinki, Finland.
- **Network:** **Cloudflare** delivers our website and sees the traffic to it.
- **Regulator websites:** **Microsoft Azure** carries our requests to regulator websites that answer only addresses in North America. These requests contain matter numbers, not your personal information.
- **Delivery:** download links are end-to-end encrypted. The key is only in the link that we send to you, so the storage never sees the files.

These providers process data for us under their own terms. Some of them are in the United States or in other countries. For this reason, your information can be processed outside your country.

## How long we keep it

| What | How long |
|---|---|
| The original email | 30 days after we complete the request. 7 days if we did not process it, for example because it failed authentication. |
| Your request record | 90 days with your address. Then we replace your address with a one-way code and remove the subject. We delete the record after 400 days. |
| Our audit log (what the system did, with your address only as a one-way code) | 400 days |
| Download links | They expire after 7 days |
| Website access logs | 30 days |
| Backups | Deleted data can stay in our encrypted backups for up to 14 days (35 days for off-site copies) |

The documents are public records. We keep them as a cache for up to 395 days after their last use.

## Your choices and rights

You can ask us for these actions:

- to tell you what we hold about you, and to give you a copy;
- to correct it;
- to delete it;
- to stop the use of it.

To delete all of your data, send an email to **agent@hsingh.app** from the same address, with **DELETE MY DATA** in the subject. We send a confirmation by email. We delete all the data that we hold about that address within 30 days (usually within 1 day). We then stop the work on mail from that address.

For other requests, or if you do not have that address now, write to **privacy@hsingh.app**. Write from the address that you used with the agent, because this is how we confirm your identity. We answer within 30 days.

If you are not satisfied, you can complain to your data protection authority. In Canada, this is the Office of the Privacy Commissioner. In the EU, this is your local supervisory authority.

## Security

We answer only authenticated email. We encrypt data in transit, the download links and the backups. We give access to our systems only to the persons who need it, and we do regular security checks. If a problem occurs that has an effect on you, we will tell you.

To report a vulnerability, send an email to **security@hsingh.app**. Do not test with the data of other people, and do not interrupt the service.

## Children

The service is for professionals who work with regulatory filings. It is not for children.

## Changes

If we make an important change to this notice, we change the date at the top and add a short description of the change here.

- 5 October 2026: TypeSafe's Jev model now classifies requests and checks the citations of summaries. On our plan, Jev is not zero data retention (refer to "Who else processes it").
