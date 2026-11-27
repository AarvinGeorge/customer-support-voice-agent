---
name: Quillstack Support
description: A calm, precise, transparent support console for chat and voice that shows its work.
---

<!-- SEED: re-run /impeccable document once there's code to capture the actual tokens and components. -->

# Design System: Quillstack Support

## 1. Overview

**Creative North Star: "The Quiet Ledger"**

A support console that behaves like a well-kept ledger: every entry legible, every decision traceable, nothing raised above the line unless it needs the reader's attention. The conversation sits on warm paper in plain ink; the work the agent does (verifying, searching, checking policy, preparing an action) is written in the margin as a quiet, collapsible trail that settles once it is done. Voice mode does not leave the ledger: the orb is the single ambient, living element, and it never competes with the words.

Density is moderate: generous line height in the conversation, tighter and tabular in the observability drawer, where numbers must read at a glance. Motion is responsive, not choreographed: feedback and state transitions only, short and eased out, in the spirit of Emil Kowalski's restraint. The system explicitly rejects the neon AI aesthetic, the generic chatbot widget, the cluttered enterprise console and SaaS landing-page tropes (PRODUCT.md anti-references).

**Key Characteristics:**
- Warm paper-light by default; a calm dark theme available (voice mode and reviewers).
- One warm accent, rare by design.
- One humanist sans for everything; mono only for IDs, latency, tokens and cost.
- Flat surfaces; depth from tone, not shadow.
- State is always spoken in words as well as color.

## 2. Colors

Restrained: warm-tinted neutrals (paper and ink) with a single deep ember accent.

### Primary
- **Ember** ([to be resolved during implementation]; a deep terracotta, low-to-mid chroma): primary action, focus ring, the currently running step, the Confirm button. Nothing else.

### Neutral
- **Paper** ([to be resolved]): the page surface; warm, never pure white.
- **Ink** ([to be resolved]): body text; warm near-black, never pure black.
- **Margin** ([to be resolved]): the slightly deeper surface of the drawer and step trail.
- **Rule** ([to be resolved]): hairline dividers and input borders.

### Named Rules
**The One Ember Rule.** The accent covers under 10 percent of any screen. If two things on screen are ember, one of them is wrong.
**The Words First Rule.** Success, warning, error and every voice state carry a text label; color only reinforces it.

## 3. Typography

**Body Font:** [humanist sans, Inter-class, to be chosen at implementation]
**Label/Mono Font:** [mono for IDs, latency, tokens and cost, to be chosen at implementation]

**Character:** One warm, highly legible sans carries the whole interface; the mono appears only where the user reads exact values.

### Hierarchy
- **Title** (semibold): screen and drawer section titles; few.
- **Body** (regular, comfortable line height, max 70ch): the conversation.
- **Label** (medium, small): step names, chips, metric captions.
- **Mono** (regular, small, tabular figures): IDs, timings, token counts, cost.

### Named Rules
**The No Display Rule.** No display fonts, no oversized headings inside the console; hierarchy comes from weight and a tight 1.125 to 1.2 scale.

## 4. Elevation

Flat by default. Surfaces separate by tone (Paper vs Margin) and hairline rules, not shadows. A single soft shadow is permitted only for the transient layer that sits above content (the drawer on narrow screens, a toast). Motion: 150 to 250 ms, exponential ease-out, transform and opacity only; the orb is the only ambient animation and is replaced by a static state ring under reduced motion.

## 5. Components

[Omitted in seed: no components exist yet. Capture from code after the first UI slice.]

## 6. Do's and Don'ts

**Do**
- Do show every step the agent takes in the collapsible trail, settled to one line ("4 steps, 2.3 s") when done.
- Do make confirmations explicit: the action, the amount, Confirm and Cancel, never buried in prose.
- Do show long IDs in full in text, even when voice says the short form.

**Don't**
- Don't use the neon AI aesthetic: no purple-blue gradients, no glowing glass panels, no sci-fi dark by default.
- Don't build a generic chatbot widget: no bubbly avatars, no emoji-heavy replies.
- Don't drift into a cluttered enterprise console: no dense grey tables, no tiny text.
- Don't use SaaS landing-page tropes: no hero metrics, no gradient text, no identical card grids.
- Don't use side-stripe accent borders, modals as a first resort, or nested cards.
