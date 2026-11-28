---
name: Quillstack Support
description: A calm, precise, transparent support console for chat and voice that shows its work.
colors:
  paper: "#FCFAF6"
  margin: "#F5F2ED"
  raised: "#FFFDFB"
  rule: "#E3DFDA"
  ink: "#241E1A"
  muted: "#68625C"
  ember: "#A74E32"
  ember-soft: "#FAE9E1"
  on-ember: "#FCFAF6"
  success: "#397247"
  warning: "#9A6418"
  error: "#AF3D36"
  info: "#406981"
  dark-paper: "#171310"
  dark-margin: "#1F1B18"
  dark-raised: "#26221F"
  dark-rule: "#36322E"
  dark-ink: "#EBE7E2"
  dark-muted: "#A39D98"
  dark-ember: "#DA8564"
  dark-ember-soft: "#3A261D"
  dark-on-ember: "#171310"
  dark-success: "#81BB8D"
  dark-warning: "#E5B568"
  dark-error: "#E47C72"
  dark-info: "#86B2CC"
typography:
  title:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "20px"
    fontWeight: 600
    lineHeight: "28px"
  heading:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "16px"
    fontWeight: 600
    lineHeight: "24px"
  body:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: "24px"
  body-sm:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: "20px"
  label:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 500
    lineHeight: "18px"
  label-sm:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 500
    lineHeight: "16px"
  caption:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: "16px"
  mono:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: "20px"
  mono-sm:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: "16px"
  mono-lg:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "20px"
    fontWeight: 500
    lineHeight: "28px"
rounded:
  sm: "6px"
  md: "10px"
  lg: "14px"
  pill: "9999px"
spacing:
  "1": "4px"
  "2": "8px"
  "3": "12px"
  "4": "16px"
  "5": "24px"
  "6": "32px"
  "7": "48px"
  "8": "64px"
components:
  button-primary:
    backgroundColor: "{colors.ember}"
    textColor: "{colors.on-ember}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
  button-ink:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
  button-quiet:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
  input-bar:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "12px 16px"
  chip:
    backgroundColor: "{colors.margin}"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "2px 10px"
---

<!-- Tokens approved by Aarvin from the Figma mockups on 2026-11-27 (D-044): https://www.figma.com/design/BYe8KiMLpuVBKozu92H8NZ . Re-run /impeccable document once there's code to capture the real components. -->

# Design System: Quillstack Support

## 1. Overview

**Creative North Star: "The Quiet Ledger"**

A support console that behaves like a well-kept ledger: every entry legible, every decision traceable, nothing raised above the line unless it needs the reader's attention. The conversation sits on warm paper in plain ink; the work the agent does (verifying, searching, checking policy, preparing an action) is written in the margin as a quiet, collapsible trail that settles once it is done. Voice mode does not leave the ledger: the orb is the single ambient, living element, and it never competes with the words.

Density is moderate: generous line height in the conversation, tighter and tabular in the observability drawer, where numbers must read at a glance. Spacing follows a 4-based scale (4, 8, 12, 16, 24, 32, 48, 64), varied for rhythm. Motion is responsive, not choreographed: feedback and state transitions only, 150 to 250 ms with exponential ease-out, in the spirit of Emil Kowalski's restraint. The system rejects the neon AI aesthetic, the generic chatbot widget, the cluttered enterprise console and SaaS landing-page tropes (PRODUCT.md anti-references).

**Key Characteristics:**
- Warm paper-light by default; a calm dark theme through the same tokens (Figma variable modes Light and Dark).
- One ember accent, rare by design.
- Inter for everything; JetBrains Mono only for IDs, timings, tokens and cost.
- Flat surfaces; depth from tone (paper, margin, raised) and hairline rules, never shadow.
- State is always spoken in words as well as color.

## 2. Colors

Restrained: warm-tinted neutrals with a single deep ember accent. OKLCH is the reference; hex is the token value.

### Primary
- **Ember** (#A74E32, oklch(0.53 0.125 38); dark #DA8564, oklch(0.70 0.115 42)): Confirm and the primary action, the focus ring, the currently running step, the orb. Nothing else.
- **Ember Soft** (#FAE9E1; dark #3A261D): tint behind the running step row and the orb glow.
- **On Ember** (#FCFAF6; dark #171310): text on ember.

### Neutral
- **Paper** (#FCFAF6, oklch(0.985 0.005 75); dark #171310): the page surface; never pure white.
- **Margin** (#F5F2ED; dark #1F1B18): drawer, step trail, user message.
- **Raised** (#FFFDFB; dark #26221F): cards and the input bar.
- **Rule** (#E3DFDA; dark #36322E): hairlines and input borders.
- **Ink** (#241E1A, oklch(0.24 0.012 60); dark #EBE7E2): body text and ink buttons; never pure black.
- **Muted** (#68625C; dark #A39D98): labels and timings.

### State
- **Success** (#397247; dark #81BB8D): gate passed, action committed.
- **Warning** (#9A6418; dark #E5B568): retrying, fallback model, pending approval.
- **Error** (#AF3D36; dark #E47C72): failed step, declined.
- **Info** (#406981; dark #86B2CC): rate-limit notice.

Contrast on Paper: ink 15.8:1 (dark 15.0), muted 5.8:1 (6.9), ember 5.3:1 (6.6), state colors 4.8:1 or higher (6.5); all pass WCAG AA for body text.

### Named Rules
**The One Ember Rule.** The accent covers under 10 percent of any screen. If two things on screen are ember, one of them is wrong.
**The Words First Rule.** Success, warning, error and every voice state carry a text label; color only reinforces it.

## 3. Typography

**Body Font:** Inter (with system-ui, sans-serif)
**Label/Mono Font:** JetBrains Mono (with ui-monospace, monospace)

**Character:** One warm, highly legible sans carries the whole interface; the mono appears only where the user reads exact values.

### Hierarchy
- **Title** (600, 20/28): screen and drawer section titles; few.
- **Heading** (600, 16/24): card titles such as "Refund $96 for the charge on 21 November".
- **Body** (400, 15/24, max 70ch): the conversation.
- **Body Small** (400, 14/20): secondary text in cards.
- **Label** (500, 13/18) and **Label Small** (500, 12/16): step names, chips, buttons, metric captions.
- **Caption** (400, 12/16): helper text under the input bar.
- **Mono** (400, 13/20), **Mono Small** (400, 12/16), **Mono Large** (500, 20/28): IDs, timings, token counts, cost; tabular figures; Mono Large for the drawer metric strip.

### Named Rules
**The No Display Rule.** No display fonts and no oversized headings inside the console; hierarchy comes from weight and a 1.125 to 1.2 scale.

## 4. Elevation

Flat everywhere. Surfaces separate by tone (Paper, Margin, Raised) and hairline Rule borders; no shadows are used in the mockups. Motion: 150 to 250 ms, exponential ease-out, transform and opacity only; the orb is the only ambient animation and becomes a static state ring with a level meter under reduced motion.

### Named Rules
**The Flat Ledger Rule.** If a surface needs a shadow to be seen, its tone is wrong.

## 5. Components

Captured from the approved mockups; refine from code with `/impeccable document`.

### Buttons
- **Shape:** gently rounded (10px).
- **Primary (ember):** Confirm only; on-ember text; 8px by 16px padding.
- **Ink:** strong secondary action ("Back to typing", "Verify").
- **Quiet:** raised surface with a Rule border (Cancel, Mute, Stop speaking, Inspector).

### Step trail
- Collapsed: one line, check icon, "4 steps, 2.3 s, Refund ready to confirm", chevron.
- Expanded: rows of icon, past-tense title, detail in mono, latency right-aligned; the running row on Ember Soft; gate rows show score against threshold and a small meter.

### Cards (verification, confirmation, citation, ticket)
- Raised surface, Rule border, 14px radius, never nested. Confirmation shows amount, invoice, rule code, expiry, Cancel and Confirm.

### Chips
- Pill shape on Margin: verified identity, ticket "ending 8213" (full ID on hover), source citation with an external link icon, state labels (Interrupted in warning).

### Voice panel
- Replaces the input bar in place: orb (ember-tinted, calm), state label with icon, two-row waveform strip (Agent, You), live caption box, Mute, Stop speaking, Back to typing.

### Observability drawer
- Right panel on Margin: metric strip in Mono Large (latency, TTFT, tokens with cached, cost), two tabs (Trace and decisions; Usage and latency); no LangSmith link (D-087).

## 6. Do's and Don'ts

**Do**
- Do show every step in the collapsible trail, settled to one line when done.
- Do make confirmations explicit: the action, the amount, Confirm and Cancel, never buried in prose.
- Do show long IDs in full in text, even when voice says the short form.

**Don't**
- Don't use the neon AI aesthetic: no purple-blue gradients, no glowing glass panels, no sci-fi dark by default.
- Don't build a generic chatbot widget: no bubbly avatars, no emoji-heavy replies.
- Don't drift into a cluttered enterprise console: no dense grey tables, no tiny text.
- Don't use SaaS landing-page tropes: no hero metrics, no gradient text, no identical card grids.
- Don't use side-stripe accent borders, modals as a first resort, shadows, or nested cards.
