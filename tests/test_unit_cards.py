"""Who is allowed to carry the Unit's identity card.

`_refresh_cards` used to write the card into every folder whose registry entry
declared the Unit. A joined member is such a folder, so renaming the Unit
re-minted the home's card — home `org_path` and all — inside a *different*
repository, and deleted cards grew back.
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lab import state, unitlink, unitmatch
from lab.workspace import init_workspace, load_unit


def make_repo(folder: Path, origin_url: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    argv = [["init", "-q"]]
    if origin_url:
        argv.append(["remote", "add", "origin", origin_url])
    for args in argv:
        subprocess.run(["git"] + args, cwd=folder, check=True, capture_output=True)
    return folder


class MemberCardScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(self.base / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.home = make_repo(self.base / "home", "https://github.com/rultrix/home-unit.git")
        init_workspace(self.home, name="home-unit")
        self.unit_id = load_unit(self.home)["unit_id"]

    def _member_with_residue_card(self) -> Path:
        """A different repository that declares this Unit (what local-lab is)."""
        member = make_repo(self.base / "member", "https://github.com/rultrix/other-repo.git")
        shutil.copy2(self.home / "groma-unit.json", member / "groma-unit.json")
        init_workspace(member, name="member")
        unitmatch.sync_units()
        return member

    def test_rename_does_not_regrow_a_card_in_another_repository(self) -> None:
        member = self._member_with_residue_card()
        self.assertEqual(load_unit(member)["unit_id"], self.unit_id)  # it really declares the Unit
        (member / "groma-unit.json").unlink()

        unitmatch.rename_unit(self.unit_id, "改名后的 Unit")

        self.assertFalse(
            (member / "groma-unit.json").exists(),
            "a member repository must not receive the home's identity card",
        )
        self.assertEqual(unitlink.read_link(self.home)["name"], "改名后的 Unit")

    def test_rename_leaves_an_existing_member_residue_card_untouched(self) -> None:
        member = self._member_with_residue_card()
        before = unitlink.read_link(member)

        unitmatch.rename_unit(self.unit_id, "改名后的 Unit")

        after = unitlink.read_link(member)
        # The fix stops propagation; it does not scrub residue. Cleaning the card
        # that is already sitting in a member repository is the 项目所有者's call
        # (AGENTS.md freezes its commit/delete/ignore), so Groma must not do it silently.
        self.assertEqual(after, before)
        self.assertNotEqual(after.get("name"), "改名后的 Unit")

    def test_a_second_clone_of_the_home_repository_still_follows_the_rename(self) -> None:
        clone = make_repo(self.base / "clone", "https://github.com/rultrix/home-unit.git")
        unitlink.adopt_link(clone, unitmatch.identity_of(self.unit_id))
        init_workspace(clone, name="home-unit-clone")
        unitmatch.sync_units()

        unitmatch.rename_unit(self.unit_id, "改名后的 Unit")

        self.assertEqual(unitlink.read_link(clone)["name"], "改名后的 Unit")

    def test_join_still_never_writes_a_card(self) -> None:
        member = make_repo(self.base / "plain", "https://github.com/rultrix/plain-repo.git")

        unitmatch.join(self.home, None, member, role="evidence")
        unitmatch.rename_unit(self.unit_id, "改名后的 Unit")

        self.assertFalse((member / "groma-unit.json").exists())
        self.assertFalse((member / ".groma").exists())


    def test_a_member_without_its_own_remote_is_not_rewritten_either(self) -> None:
        """No remote (or a local-path one) means the org path is unknowable.

        Unknowable must stop the refresh, not default to it: this is the shape
        that kept absorbing the home's identity into an unrelated repository.
        """
        member = make_repo(self.base / "noremote")
        shutil.copy2(self.home / "groma-unit.json", member / "groma-unit.json")
        init_workspace(member, name="noremote")
        unitmatch.sync_units()
        before = unitlink.read_link(member)

        unitmatch.rename_unit(self.unit_id, "改名后的 Unit")

        self.assertEqual(unitlink.read_link(member), before)


    def test_rename_keeps_the_home_cards_creation_time(self) -> None:
        """A regenerated card silently reset `created_at` in both sandboxes.

        The Unit's age is derived from the earliest card, so losing it rewrites
        which folder is the home. Nothing should refresh that field.
        """
        before = unitlink.read_link(self.home)

        unitmatch.rename_unit(self.unit_id, "改名后的 Unit")

        after = unitlink.read_link(self.home)
        self.assertEqual(after["created_at"], before["created_at"])
        self.assertEqual(after["name"], "改名后的 Unit")


    def test_a_deleted_home_card_comes_back_with_a_fresh_creation_time(self) -> None:
        """Pin the asymmetry, so nobody deletes the wrong card by accident.

        A member's deleted card stays gone; the home's is regenerated — with a new
        `created_at`, which is what `_card_created` uses to decide which folder is
        the home. The old stamp is fixed in the past so the comparison cannot land
        in the same second by accident.
        """
        unitmatch.sync_units()  # the registry knows the home, as in real use
        card = self.home / "groma-unit.json"
        stale = json.loads(card.read_text(encoding="utf-8"))
        stale["created_at"] = "2020-01-01T00:00:00Z"
        card.write_text(json.dumps(stale, ensure_ascii=False, sort_keys=True) + "\n",
                        encoding="utf-8", newline="\n")
        card.unlink()

        unitmatch.rename_unit(self.unit_id, "改名后的 Unit")

        regenerated = unitlink.read_link(self.home)
        self.assertIsNotNone(regenerated, "the home card is regenerated by the rename path")
        self.assertEqual(regenerated["unit_id"], self.unit_id)
        self.assertNotEqual(regenerated["created_at"], "2020-01-01T00:00:00Z")

    def test_renaming_without_a_home_never_promotes_a_member(self) -> None:
        """Defensive pin — also passes before the hardening, so no bug is claimed.

        `home_path()` falls back to the first member when nothing is declared,
        which is right for display and wrong as a write target; refreshing now uses
        the declared home only. I could not make the fallback cause harm on this or
        the previous HEAD, so this only keeps it from ever becoming reachable.
        """

        unitmatch.sync_units()
        member = self._member_with_residue_card()
        (member / "groma-unit.json").unlink()          # only the home's card exists
        unitmatch.sync_units()
        state.remove_workspace(self.home, delete_evidence=False)  # home_binding -> None

        unitmatch.rename_unit(self.unit_id, "改名后的 Unit")

        self.assertFalse((member / "groma-unit.json").exists())
        self.assertIsNone(state.unit_record(self.unit_id).get("home_binding"))

    def test_renaming_an_unknown_unit_fails_cleanly_instead_of_crashing(self) -> None:
        """No registry record and no card: say so, do not raise KeyError."""
        from lab.workspace import LabError

        (self.home / "groma-unit.json").unlink()

        with self.assertRaises(LabError) as error:
            unitmatch.rename_unit("unit_not_on_this_machine", "改名")
        self.assertIn("无法确定家目录", str(error.exception))


    def test_a_joined_member_is_not_promoted_when_the_home_vanishes(self) -> None:
        """Defensive pin, not a proven bug: this also passes before the hardening.

        I expected `home_path()`'s member fallback to hand a joined repository the
        home's identity card once nothing was declared. It did not, on this or the
        previous HEAD, so the claim is not made — the assertion only keeps the
        fallback from ever becoming reachable in this shape.
        """
        joined = make_repo(self.base / "joined", "https://github.com/rultrix/joined-repo.git")
        init_workspace(joined, name="joined")          # its own Unit at first
        unitmatch.join(self.home, None, joined, role="evidence")
        unitmatch.sync_units()
        state.remove_workspace(self.home, delete_evidence=False)   # home_binding -> None

        unitmatch.rename_unit(self.unit_id, "改名后的 Unit")

        link = unitlink.read_link(joined)
        self.assertTrue(
            link is None or link.get("unit_id") != self.unit_id,
            "a joined member must not be handed the home's identity card",
        )


if __name__ == "__main__":
    unittest.main()
