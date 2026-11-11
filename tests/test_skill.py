"""The skill's header must load in every agent that reads it, not only the most lenient one."""

import re
from pathlib import Path

SKILLS = sorted(Path(__file__).parents[1].glob("skills/*/SKILL.md"))
MAX_DESCRIPTION = 1024  # agents refuse to load a skill whose description is longer


def test_descriptions_are_short_enough_and_plain_yaml():
    assert SKILLS
    for path in SKILLS:
        header = path.read_text(encoding="utf-8").split("---")[1]
        fields = dict(re.findall(r"^(\w+): (.*)$", header, re.MULTILINE))
        description = fields["description"]
        assert len(description) <= MAX_DESCRIPTION, (path, len(description))
        # In a plain (unquoted) YAML value, ": " starts a mapping and " #" a comment.
        assert ": " not in description and " #" not in description, path
        assert not description.startswith(('"', "'", ">", "|")), path


def test_every_file_a_skill_points_to_exists():
    """Parts read only when needed live in references/; a pointer to a missing one is a dead end."""
    for path in SKILLS:
        for ref in set(re.findall(r"references/[\w.-]+\.md", path.read_text(encoding="utf-8"))):
            assert (path.parent / ref).is_file(), (path, ref)
