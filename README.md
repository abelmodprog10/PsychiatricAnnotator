# Clinical file annotation – demo

Prototype of the observation-log design: the clinical file on the left, the scoring boxes on the right.

## Run

    pip install flask
    python app.py          # then open http://127.0.0.1:5000

## How it works

- **Left:** documents of the selected donor (`donors/<id>/*.txt`). Tick *no relevant clinical information* or *poorly legible* per document, so silence can be told apart from unreadable or irrelevant material.
- **Middle:** the document. Select text to attach it as evidence (character offsets are stored and the span is highlighted afterwards), or type a quote by hand. Click a highlight to edit it.
- **Observations tab:** one row per document × item. Type (symptom / diagnosis / event / substance), item from a drop-down (with "other, specify" entries), certainty, who asserted it, severity with the item's anchors, period referred to, first-observation flag, recapitulated vs new, retrospective vs contemporaneous, and a change flag. Explicit negatives are recorded with certainty *explicitly absent*; no row means not documented.
- **Medication tab:** one row per drug trial from a drug drop-down (grouped by class) or "other".
- **Measurements tab:** GAF, HDRS, MADRS, MMSE, BMI and so on, each with value, date and evidence.
- **Donor tab:** things that really are once per donor, plus a small summary layer (final diagnosis, lifetime-worst per domain) to validate the derived values against.
- **Derived tab:** computed from the log, never entered by hand: peak severity and share of documents per domain, first year with its basis, changes, a recap-only warning, medication trials and an antidepressant non-response count, document coverage gaps and the comparison with the summary layer.

Annotations are saved automatically to `data/<donor>__<rater>.json`, so several raters can score the same donor side by side. CSV export per table is in the header.

## Adding donors

Create `donors/<id>/` with `.txt` files in this format (the header keys are in Dutch because the demo files are):

    DATUM: 2004-09-14
    TYPE: brief GGZ
    AUTEUR: psychiater
    ---
    free text of the document

## Where to change the item list

`app.py`: `ITEMS`, `DIAGNOSES`, `DRUGS`, `INSTRUMENTS` and `DONOR_FIELDS` (the codebook). `CODEBOOK_VERSION` is stored with every export.

All donor files are fictional.
