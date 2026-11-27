---
article_id: integrations-sync-troubleshooting
title: "Integrations and sync troubleshooting"
source_url: https://app.notion.com/p/4257c83d58968116a1b0ce4196664e07
last_edited_time: 2026-11-27T05:26:00.807Z
tiers: [all]
---

Quillstack integrations keep your docs in step with the other tools your team uses, such as Slack, Google Drive, GitHub and Jira. This article explains how sync works and how to fix each sync error code.

## How sync works

When you connect an integration, Quillstack uses the access you grant to read items from that tool and keep linked pages up to date. Sync runs automatically in the background. You can also open the integration under Settings, then Integrations, and choose **Sync now**.

Each integration shows its last sync time and, if something went wrong, an error code.

## Check the basics first

Many sync problems have a simple cause:

- The person who connected the integration has left the company or lost access in the other tool.
- The folder, channel or project was never shared with the integration.
- The other tool is having an outage.

## Error codes

### E-SYNC-204

**What it means:** the integration is connected and working, but the last sync found nothing to bring in. Usually the folder, channel or project you expected was not shared with the integration, or the integration was given access to a different one.

**How to fix it:** in the other tool, share the folder, channel or project with the Quillstack integration. Then choose **Sync now** in Quillstack.

### E-SYNC-401

**What it means:** the connection has expired or was revoked, so Quillstack can no longer read from the other tool.

**How to fix it:** open the integration in Quillstack and choose **Reconnect**. Sign in to the other tool and approve access again.

### E-SYNC-403

**What it means:** Quillstack can reach the other tool, but does not have permission to read a specific item.

**How to fix it:** check that the person who connected the integration can open the item in the other tool. If not, share it with them or reconnect with an account that has access.

### E-SYNC-409

**What it means:** the same content was changed in Quillstack and in the other tool at the same time, so sync paused that item to avoid overwriting either change.

**How to fix it:** open the item in Quillstack, review both versions, keep the one you want, then choose **Sync now**.

### E-SYNC-429

**What it means:** the other tool asked Quillstack to slow down because too many requests were sent in a short time.

**How to fix it:** you do not need to do anything. Sync waits and tries again automatically. If the error is still showing the next day, contact support.

### E-SYNC-503

**What it means:** the other tool did not respond, usually because it is having an outage.

**How to fix it:** check the other tool's status page. Sync tries again automatically once the tool is available.

## Reconnecting an integration

Reconnecting is safe. It keeps your existing linked pages and replaces only the stored access.

1. Open Settings, then Integrations.
2. Choose the integration and select **Reconnect**.
3. Sign in to the other tool and approve the access Quillstack asks for.
4. Choose **Sync now** and check that the error has cleared.

## When to contact support

Contact support if:

- you see an error code that is not listed in this article
- the same error comes back after you have followed the fix
- sync seems to have lost or changed content

Include the integration name, the error code and the steps you have already tried. We open a ticket in our technical queue with those details, so nobody asks you to repeat them.
