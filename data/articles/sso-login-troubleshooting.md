---
article_id: sso-login-troubleshooting
title: "SSO and login troubleshooting"
source_url: https://app.notion.com/p/4257c83d589681d586feca20a38dccaf
last_edited_time: 2026-11-27T05:25:37.865Z
tiers: [all]
---

This article helps you fix problems signing in to Quillstack, with or without single sign-on (SSO). Each error code below has its own section, so you can search for the code you see.

## Before you troubleshoot

Note these details first. They make most problems quick to fix:

- the exact error code on screen, for example `E-SSO-401`
- the email address you used
- whether anyone else on your team sees the same error
- whether you sign in with SSO or with a password

## Error codes

### E-SSO-401

**What it means:** Quillstack could not verify the sign-in response from your identity provider. This usually happens when the signing certificate in Quillstack does not match the one your identity provider uses, for example after the certificate was renewed.

**How to fix it:** an account owner downloads the current metadata file from your identity provider and uploads it again under Settings, then Security, then Single sign-on.

### E-SSO-403

**What it means:** your identity provider signed you in, but you are not assigned to the Quillstack application there.

**How to fix it:** ask your identity provider admin to assign you, or a group you belong to, to the Quillstack application.

### E-SSO-404

**What it means:** Quillstack found no SSO connection for your email domain. The domain may not be verified, or SSO may not be set up yet.

**How to fix it:** check that you used your work email address. An account owner can confirm the domain is verified in the SSO settings.

### E-SSO-408

**What it means:** the sign-in request expired before it finished. This can happen if the sign-in page was left open for a long time, or if the clock on your identity provider's server is wrong.

**How to fix it:** close the tab and start the sign-in again from the Quillstack sign-in page. If it keeps happening, ask your identity provider admin to check the server time.

### E-SSO-422

**What it means:** the sign-in response did not include an email address, or the email does not match a person in your workspace.

**How to fix it:** ask your identity provider admin to send the user's email address as the NameID. See SSO setup for the exact setting in Okta and Azure AD.

### E-LOGIN-429

**What it means:** there were too many sign-in attempts in a short time, so sign-in is paused for your account.

**How to fix it:** wait a few minutes, then try again once. If you have forgotten your password, reset it instead of trying again.

## Password sign-in problems

If your workspace does not use SSO:

1. On the sign-in page, choose "Forgot password" and enter your work email.
2. Check your inbox, including spam, for the reset email.
3. Use the newest reset link. Older links stop working once a new one is sent.

If your workspace requires SSO, password sign-in is turned off. Sign in through your identity provider instead.

## Support PIN and verification problems

Your support PIN is separate from your sign-in password. Support uses your account ID and your 4-digit support PIN to confirm who you are.

- If verification fails 3 times in one conversation, support opens a ticket so a person can help you.
- After 5 failed attempts within 15 minutes, verification for the account is paused for 15 minutes.

## When SSO is broken for your whole company

If nobody at your company can sign in, contact support straight away and say that the whole company is affected. We open a ticket in our technical queue. On Enterprise, a problem that blocks your team is handled at our highest priority. See Support, response times and escalation.

## What to include when you contact support

- the error code and the time it happened
- your email address and account ID
- what you have already tried from this article

This helps our team pick up where you left off instead of repeating steps.
