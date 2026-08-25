"""Facet templates: the default breakdown of a subject by type.

This is deliberately plain data. Adding a new subject type — statute, formula,
painting, grammar point — means editing this dict, never touching the schema.
Templates are only a starting proposal: capture may override them per subject.

`cue` tells the tutor how to *ask*, not what the answer is. Varying the wording
between reviews is the point; drilling a fixed question teaches you the question.

On choosing a mode: `recall` is for a facet with ONE fact in it -- a word and
its meaning. The moment an answer has several parts, it is a `list`, even where
those parts read naturally as a single sentence. Prose that holds four facts is
still four facts, and only a list lets them be marked off one at a time.

On overlap: sibling facets should be separate retrieval paths, not restatements
-- a cast list next to a narrative facet is scheduled only when it carries
figures the narrative wouldn't force out (see the CAPTURING rules in the server
instructions, which own this judgment). These templates propose the axes; the
model decides per subject which ones deserve scheduling.
"""

from __future__ import annotations

GRADING_MODES = ("recall", "list", "open")

TEMPLATES: dict[str, list[dict]] = {
    "person": [
        {
            # A list, not a recall: role, dates and affiliations are separate
            # facts that happen to fit in one sentence, and storing them as
            # one string means coverage has to be re-derived from prose every
            # review. Splitting them is what lets `covered` do its job.
            "name": "identity",
            "grading_mode": "list",
            "cue": "Who were they, when and where did they live?",
        },
        {
            "name": "contributions",
            "grading_mode": "list",
            "cue": "What did they actually do? Ask for the specifics, not a summary.",
        },
        {
            "name": "significance",
            "grading_mode": "list",
            "scheduled": False,
            "cue": "Why they still matter — context for the other facets.",
        },
    ],
    "myth": [
        {
            "name": "story",
            "grading_mode": "list",
            "cue": "Ask them to tell the story. Look for the key beats in order.",
        },
        {
            "name": "figures",
            "grading_mode": "list",
            "cue": "Who is involved and what is each one's role?",
        },
        {
            "name": "meaning",
            "grading_mode": "open",
            "cue": "What is the myth about? Accept any defensible reading that engages the story.",
        },
    ],
    "word": [
        {
            "name": "definition",
            "grading_mode": "recall",
            "cue": "Give the word, ask for a precise definition including register or connotation.",
        },
        {
            "name": "production",
            "grading_mode": "recall",
            "cue": "Describe the meaning WITHOUT using the word; ask them to name it.",
        },
        {
            "name": "usage",
            "grading_mode": "open",
            "cue": "Ask for a sentence about something real and current, not a dictionary example.",
        },
    ],
    "concept": [
        {
            "name": "explanation",
            "grading_mode": "recall",
            "cue": "Ask them to explain it plainly, as if to a smart person who has not met it.",
        },
        {
            "name": "application",
            "grading_mode": "open",
            "cue": "Invent a FRESH scenario each time and ask them to apply the concept to it.",
        },
    ],
    "case": [
        {"name": "facts", "grading_mode": "list", "cue": "What happened, procedurally and factually?"},
        {"name": "holding", "grading_mode": "recall", "cue": "What did the court actually decide?"},
        {"name": "rule", "grading_mode": "recall", "cue": "What rule does the case establish?"},
        {
            "name": "significance",
            "grading_mode": "open",
            "scheduled": False,
            "cue": "Why the case matters — context for the other facets.",
        },
    ],
    "event": [
        {"name": "when", "grading_mode": "recall", "cue": "When and where did it happen?"},
        {"name": "causes", "grading_mode": "list", "cue": "What led to it?"},
        {"name": "consequences", "grading_mode": "list", "cue": "What followed from it?"},
    ],
}

DEFAULT_TEMPLATE = [
    {"name": "core", "grading_mode": "recall", "cue": "Ask for the essential fact."},
    {"name": "detail", "grading_mode": "list", "cue": "Ask for the supporting specifics."},
]


def template_for(subject_type: str) -> list[dict]:
    """The proposed facet set for a type, with defaults filled in."""
    raw = TEMPLATES.get(subject_type, DEFAULT_TEMPLATE)
    out = []
    for spec in raw:
        out.append(
            {
                "name": spec["name"],
                "grading_mode": spec.get("grading_mode", "recall"),
                "scheduled": spec.get("scheduled", True),
                "cue": spec.get("cue"),
            }
        )
    return out


def known_types() -> list[str]:
    return sorted(TEMPLATES)
