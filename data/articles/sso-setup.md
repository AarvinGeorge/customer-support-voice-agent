---
article_id: sso-setup
title: "SSO setup (SAML, Okta, Azure AD)"
source_url: https://app.notion.com/p/4257c83d589681879c5ef2b9c9f82325
last_edited_time: 2026-11-27T05:25:37.865Z
tiers: [business, enterprise]
---

Single sign-on (SSO) lets your team sign in to Quillstack with your company identity provider instead of a separate password. This article explains how to set up SAML SSO, with notes for Okta and Azure AD (Microsoft Entra ID).

## Who can use SSO

SSO is available on the Business and Enterprise plans. It is not available on Free or Pro.

Only an account owner can set up or change SSO for the workspace.

## Before you start

Have these ready:

- Admin access to your identity provider, for example Okta or Azure AD.
- The email domain your team uses to sign in, for example [example.com](http://example.com).
- A test user in your identity provider who is also a member of your Quillstack workspace.

## Set up SAML SSO

1. In Quillstack, open Settings, then Security, then Single sign-on.
2. Copy the two values Quillstack shows: the **ACS URL** (where your identity provider sends sign-in responses) and the **Entity ID** (the name that identifies Quillstack to your identity provider).
3. In your identity provider, create a new SAML application for Quillstack and paste in the ACS URL and the Entity ID.
4. Set the user's email address as the NameID, in email format. Quillstack matches people by email address.
5. Assign the people or groups who should use Quillstack to the new application.
6. Download the identity provider metadata file, or copy its SSO URL and signing certificate.
7. Back in Quillstack, upload the metadata file, or paste the SSO URL and certificate, then save.
8. Add and verify your email domain so Quillstack knows which people should sign in with SSO.

### Okta

- In the Okta admin console, go to Applications and create a new app integration of type SAML 2.0.
- Paste the ACS URL into "Single sign-on URL" and the Entity ID into "Audience URI".
- Set "Name ID format" to EmailAddress and "Application username" to Email.
- After saving, open the Sign On tab to download the metadata file.

### Azure AD (Microsoft Entra ID)

- In the Entra admin center, go to Enterprise applications and create your own non-gallery application.
- Under Single sign-on, choose SAML.
- Paste the Entity ID into "Identifier" and the ACS URL into "Reply URL".
- Make sure the "Unique User Identifier" claim uses the user's email address.
- Download the Federation Metadata XML and upload it to Quillstack.

## Test before you enforce

Test with your test user before you turn SSO on for everyone:

1. Open a private browser window.
2. Sign in to Quillstack with the test user's email address.
3. Confirm you are sent to your identity provider and back into Quillstack.

When the test works, you can turn on **Require SSO** so everyone on your verified domain signs in through your identity provider.

## After setup

- New people get access when you assign them to the Quillstack application in your identity provider.
- Removing someone from the application stops them signing in with SSO.
- If your identity provider's signing certificate changes, upload the new metadata to Quillstack before the old certificate stops working.

If sign-in fails, see SSO and login troubleshooting, which lists each error code and its fix.
