"""auto-merge/auto_merge.py（自動マージの判定）のテスト。"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "auto-merge"))
import auto_merge as m  # noqa: E402

HEAD = "h" * 40


def codex_comment(sha, status="completed", findings=False, user=m.CODEX_BOT):
    state = {"code_review": {"commit_sha": sha, "status": status, "has_any_findings": findings}}
    return {
        "user": {"login": user},
        "body": f"まとめ\n<!-- codex-review-state:v1 {json.dumps(state)} -->",
    }


def facts(**over):
    base = {
        "owner": "o",
        "label_actor": "o",
        "owner_comments": 0,
        "codex": True,
        "test": True,
        "careful": [],
        "mergeable": True,
    }
    base.update(over)
    return base


class IssueNumberTest(unittest.TestCase):
    def test_branch_and_closing_agree(self):
        self.assertEqual(m.issue_number_of("issue-12-add-x", [12]), 12)

    def test_branch_points_elsewhere(self):
        self.assertIsNone(m.issue_number_of("issue-12-add-x", [13]))

    def test_closes_two_issues(self):
        self.assertIsNone(m.issue_number_of("issue-12-add-x", [12, 13]))

    def test_not_an_agent_branch(self):
        self.assertIsNone(m.issue_number_of("feature/x", [12]))


class LabelActorTest(unittest.TestCase):
    def test_latest_labeled_event_wins(self):
        events = [
            {"event": "labeled", "label": {"name": "merge:auto"}, "actor": {"login": "o"}},
            {"event": "unlabeled", "label": {"name": "merge:auto"}, "actor": {"login": "o"}},
            {"event": "labeled", "label": {"name": "merge:auto"}, "actor": {"login": "agent"}},
            {"event": "labeled", "label": {"name": "agent:ready"}, "actor": {"login": "o"}},
        ]
        self.assertEqual(m.label_added_by(events, "merge:auto"), "agent")

    def test_never_labeled(self):
        self.assertIsNone(m.label_added_by([], "merge:auto"))


class CodexTest(unittest.TestCase):
    def test_clean_on_head(self):
        self.assertTrue(m.codex_clean(m.codex_state([codex_comment(HEAD)]), HEAD))

    def test_findings(self):
        self.assertFalse(m.codex_clean(m.codex_state([codex_comment(HEAD, findings=True)]), HEAD))

    def test_older_commit_or_running_is_not_yet(self):
        self.assertIsNone(m.codex_clean(m.codex_state([codex_comment("old")]), HEAD))
        self.assertIsNone(m.codex_clean(m.codex_state([codex_comment(HEAD, "running")]), HEAD))
        self.assertIsNone(m.codex_clean(None, HEAD))

    def test_completed_without_result_is_not_clean(self):
        state = {"commit_sha": HEAD, "status": "completed", "has_any_findings": None}
        self.assertIsNone(m.codex_clean(state, HEAD))

    def receipt(
        self,
        sha,
        text="Codex Review: Didn't find any major issues. Another round soon, please!",
        user=m.CODEX_BOT,
    ):
        return {
            "user": {"login": user},
            "body": f"<!-- codex-review-receipt:v1 -->\n\n{text}\n\n**Reviewed commit:** `{sha[:10]}`",
        }

    def test_receipt_fills_empty_result(self):
        null = {
            "user": {"login": m.CODEX_BOT},
            "body": codex_comment(HEAD)["body"].replace("false", "null"),
        }
        self.assertIsNone(m.codex_result([null], HEAD))
        self.assertTrue(m.codex_result([null, self.receipt(HEAD)], HEAD))

    def test_receipt_must_match_commit_account_and_text(self):
        null = {
            "user": {"login": m.CODEX_BOT},
            "body": codex_comment(HEAD)["body"].replace("false", "null"),
        }
        self.assertIsNone(m.codex_result([null, self.receipt("o" * 40)], HEAD))
        self.assertIsNone(m.codex_result([null, self.receipt(HEAD, user="nosuke223-agent")], HEAD))
        self.assertIsNone(
            m.codex_result([null, self.receipt(HEAD, text="Here are some suggestions")], HEAD)
        )

    def test_receipt_does_not_override_findings(self):
        findings = codex_comment(HEAD, findings=True)
        self.assertFalse(m.codex_result([findings, self.receipt(HEAD)], HEAD))

    def test_state_from_other_accounts_is_ignored(self):
        self.assertIsNone(m.codex_state([codex_comment(HEAD, user="nosuke223-agent")]))

    def test_latest_state_wins(self):
        state = m.codex_state([codex_comment(HEAD, findings=True), codex_comment(HEAD)])
        self.assertTrue(m.codex_clean(state, HEAD))


class TestResultTest(unittest.TestCase):
    def test_states(self):
        ok = {"name": "test", "status": "completed", "conclusion": "success"}
        bad = {"name": "test", "status": "completed", "conclusion": "failure"}
        running = {"name": "test", "status": "in_progress", "conclusion": None}
        other = {"name": "judge", "status": "completed", "conclusion": "failure"}
        self.assertTrue(m.test_result([ok, other]))
        self.assertFalse(m.test_result([bad]))
        self.assertIsNone(m.test_result([running]))
        self.assertIsNone(m.test_result([other]))


class CarefulFilesTest(unittest.TestCase):
    def test_rules_settings_ci_and_dependencies(self):
        files = [
            "src/a.py",
            ".github/workflows/x.yml",
            ".claude/settings.json",
            "pyproject.toml",
            "uv.lock",
            "CLAUDE.md",
            "AGENTS.md",
            ".env.local",
            "README.md",
            "docs/guide.md",
        ]
        self.assertEqual(m.careful_files(files), files[1:8])

    def test_nested_rules_settings_and_dependencies(self):
        # 下の階層の CLAUDE.md や .claude/ もエージェントのルールになり、package.json なども依存になる
        files = [
            "src/AGENTS.md",
            "docs/CLAUDE.md",
            "web/package.json",
            "web/.claude/settings.json",
            "app/.env",
            "src/claude.ts",
            "src/.claude-notes.md",
            "sub/.github/x.yml",
        ]
        self.assertEqual(m.careful_files(files), files[:5])

    def test_web_dependency_files(self):
        files = ["src/App.tsx", "package.json", "package-lock.json", "web/package.json", ".npmrc"]
        self.assertEqual(m.careful_files(files), files[1:])


class ChangedPathsTest(unittest.TestCase):
    def test_renamed_rule_file_is_careful(self):
        files = [
            {
                "filename": "docs/agent-rules.md",
                "previous_filename": "AGENTS.md",
                "status": "renamed",
            },
            {"filename": "src/a.py", "status": "modified"},
        ]
        self.assertEqual(m.changed_paths(files), ["docs/agent-rules.md", "AGENTS.md", "src/a.py"])
        self.assertEqual(m.careful_files(m.changed_paths(files)), ["AGENTS.md"])


class JudgeTest(unittest.TestCase):
    def test_all_conditions_met(self):
        self.assertEqual(m.judge(facts()), ("ok", []))

    def test_wait_until_codex_test_and_mergeable_are_known(self):
        for over in [{"codex": None}, {"codex": False}, {"test": None}, {"mergeable": None}]:
            self.assertEqual(m.judge(facts(**over))[0], "wait", over)

    def test_each_failed_condition_is_a_reason(self):
        verdict, reasons = m.judge(
            facts(
                label_actor="agent",
                owner_comments=2,
                test=False,
                careful=["CLAUDE.md"],
                mergeable=False,
            )
        )
        self.assertEqual(verdict, "ng")
        self.assertEqual(len(reasons), 5)
        self.assertIn("agent", reasons[0])
        self.assertIn("2 件", reasons[1])
        self.assertIn("CLAUDE.md", reasons[3])


class CommentTest(unittest.TestCase):
    def test_marker_prevents_second_comment_for_same_result(self):
        body = m.comment_body("ok", [], HEAD)
        self.assertTrue(
            m.already_commented([{"user": {"login": m.ACTIONS_BOT}, "body": body}], HEAD, "ok", [])
        )
        self.assertFalse(
            m.already_commented(
                [{"user": {"login": m.ACTIONS_BOT}, "body": body}], "n" * 40, "ok", []
            )
        )

    def test_comment_again_when_result_changes_on_same_commit(self):
        ng = m.comment_body("ng", ["GitHub 上のテストか lint が失敗している"], HEAD)
        self.assertFalse(
            m.already_commented([{"user": {"login": m.ACTIONS_BOT}, "body": ng}], HEAD, "ok", [])
        )
        ok = m.comment_body("ok", [], HEAD)
        self.assertTrue(
            m.already_commented(
                [
                    {"user": {"login": m.ACTIONS_BOT}, "body": ng},
                    {"user": {"login": m.ACTIONS_BOT}, "body": ok},
                ],
                HEAD,
                "ok",
                [],
            )
        )
        # 結果が ok から ng に戻ったら、最後のコメントと比べてまたコメントする
        self.assertFalse(
            m.already_commented(
                [
                    {"user": {"login": m.ACTIONS_BOT}, "body": ng},
                    {"user": {"login": m.ACTIONS_BOT}, "body": ok},
                ],
                HEAD,
                "ng",
                ["x"],
            )
        )

    def test_reasons_change_is_a_new_result(self):
        body = m.comment_body("ng", ["理由A"], HEAD)
        self.assertFalse(
            m.already_commented(
                [{"user": {"login": m.ACTIONS_BOT}, "body": body}], HEAD, "ng", ["理由A", "理由B"]
            )
        )

    def test_reasons_are_listed(self):
        body = m.comment_body("ng", ["理由A", "理由B"], HEAD)
        self.assertIn("- 理由A\n- 理由B", body)
        self.assertIn("試運転", body)


class MarkerTest(unittest.TestCase):
    def test_reads_verdict_and_mode(self):
        m_ = m.last_marker(
            [{"user": {"login": m.ACTIONS_BOT}, "body": m.comment_body("ng", ["x"], HEAD, "on")}],
            HEAD,
        )
        self.assertEqual((m_.group(3), m_.group(4)), ("ng", "on"))

    def test_reads_old_marker_without_verdict(self):
        old = {
            "user": {"login": m.ACTIONS_BOT},
            "body": f"<!-- auto-merge-check sha={HEAD} result=abc -->",
        }
        self.assertTrue(m.already_commented([old], HEAD, "x", []) is False)
        self.assertEqual(m.last_marker([old], HEAD).group(2), "abc")

    def test_markers_from_other_accounts_are_ignored(self):
        forged = {"user": {"login": "someone"}, "body": m.comment_body("error", ["x"], HEAD, "on")}
        self.assertIsNone(m.last_marker([forged], HEAD))
        self.assertFalse(m.merge_failed([forged], HEAD))
        ok = {"user": {"login": "nosuke223-agent"}, "body": m.comment_body("ok", [], HEAD, "on")}
        self.assertFalse(m.already_commented([ok], HEAD, "ok", [], "on"))

    def test_merge_failed_only_for_same_commit(self):
        error = {
            "user": {"login": m.ACTIONS_BOT},
            "body": m.comment_body("error", ["denied"], HEAD, "on"),
        }
        self.assertTrue(m.merge_failed([error], HEAD))
        self.assertFalse(m.merge_failed([error], "n" * 40))

    def test_texts_by_mode(self):
        self.assertIn("試運転", m.comment_body("ok", [], HEAD))
        self.assertIn("マージしました", m.comment_body("ok", [], HEAD, "on"))
        self.assertIn("- denied", m.comment_body("error", ["denied"], HEAD, "on"))


class MainTest(unittest.TestCase):
    PR = {
        "number": 7,
        "user": {"login": "agent"},
        "head": {"sha": HEAD, "ref": "issue-3-x", "repo": {"full_name": "o/r"}},
        "base": {"ref": "main"},
    }

    def run_main(
        self, mode, verdict=("ok", []), comments=(), merge_error=None, pr=None, merged=True
    ):
        calls = []

        def gh(*args):
            calls.append(args)
            if merge_error and args[:2] == ("pr", "merge"):
                raise subprocess.CalledProcessError(1, "gh", stderr=merge_error)

        env = {"AUTO_MERGE": mode, "REPO": "o/r", "AGENT_ACCOUNT": "agent"}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(
                m,
                "gh_api",
                side_effect=lambda path, paginate=False: (
                    {"merged": merged}
                    if path == "repos/o/r/pulls/7"
                    else [pr or self.PR]
                    if path.startswith("repos/o/r/pulls")
                    else {"default_branch": "main"}
                    if path == "repos/o/r"
                    else {
                        "state": "open",
                        "labels": [{"name": "agent:review"}, {"name": "merge:auto"}],
                    }
                ),
            ),
            mock.patch.object(m, "collect", return_value=(3, {"pr_comments": list(comments)})),
            mock.patch.object(m, "judge", return_value=verdict),
            mock.patch.object(m, "gh", side_effect=gh),
            mock.patch(
                "subprocess.run",
                side_effect=lambda cmd, **kw: (
                    calls.append(tuple(cmd[1:]))
                    or subprocess.CompletedProcess(
                        cmd, 0, json.dumps({"object": {"sha": HEAD}}), ""
                    )
                ),
            ),
        ):
            m.main()
        return calls

    def test_retry_runs_only_when_merging_is_on(self):
        with mock.patch.object(m, "retry_unfinished") as retry:
            self.run_main("dry-run")
            retry.assert_not_called()
            self.run_main("on")
            retry.assert_called_once_with("o/r", "agent", "main")

    def test_cleanup_waits_until_actually_merged(self):
        # マージキューでは、マージのコマンドが成功しても、まだマージされていない
        calls = self.run_main("on", merged=False)
        self.assertEqual([c[:2] for c in calls], [("pr", "review"), ("pr", "merge")])

    def test_pr_to_another_branch_is_skipped(self):
        other = dict(self.PR, base={"ref": "feature/x"})
        self.assertEqual(self.run_main("on", pr=other), [])

    def test_forged_success_comment_does_not_skip_merge(self):
        forged = {
            "user": {"login": "nosuke223-agent"},
            "body": m.comment_body("ok", [], HEAD, "on"),
        }
        calls = self.run_main("on", comments=[forged])
        self.assertIn(("pr", "merge"), [c[:2] for c in calls])

    def test_dry_run_only_comments(self):
        calls = self.run_main("dry-run")
        self.assertEqual([c[:2] for c in calls], [("pr", "comment")])

    def test_on_approves_merges_comments_closes_and_removes_labels(self):
        calls = self.run_main("on")
        # 後片付け（Issue を閉じる、ラベル、ブランチ）が済んでから、済んだ記録のコメントを書く
        self.assertEqual(
            [c[:2] for c in calls],
            [
                ("pr", "review"),
                ("pr", "merge"),
                ("issue", "close"),
                ("issue", "edit"),
                ("api", "repos/o/r/git/refs/heads/issue-3-x"),
                ("api", "-X"),
                ("pr", "comment"),
            ],
        )
        self.assertEqual(calls[5][-1], "repos/o/r/git/refs/heads/issue-3-x")
        self.assertIn("--match-head-commit", calls[1])
        self.assertIn(HEAD, calls[1])
        self.assertEqual(calls[2][:3], ("issue", "close", "3"))
        self.assertEqual(calls[2][-2:], ("--reason", "completed"))
        self.assertEqual(calls[3][-1], "agent:review")
        self.assertIn("verdict=ok mode=on", calls[6][-1])

    def test_merge_failure_is_commented_once(self):
        calls = self.run_main("on", merge_error="review required")
        self.assertIn("review required", calls[-1][-1])
        self.assertIn("verdict=error", calls[-1][-1])
        again = self.run_main(
            "on", comments=[{"user": {"login": m.ACTIONS_BOT}, "body": calls[-1][-1]}]
        )
        self.assertEqual(again, [])

    def test_dry_run_ok_does_not_stop_merging_after_switch_to_on(self):
        dry = {"user": {"login": m.ACTIONS_BOT}, "body": m.comment_body("ok", [], HEAD)}
        calls = self.run_main("on", comments=[dry])
        self.assertEqual([c[:2] for c in calls][:2], [("pr", "review"), ("pr", "merge")])
        self.assertEqual(self.run_main("dry-run", comments=[dry]), [])

    def test_failure_is_remembered_after_other_verdicts(self):
        error = {
            "user": {"login": m.ACTIONS_BOT},
            "body": m.comment_body("error", ["review required"], HEAD, "on"),
        }
        ng = {
            "user": {"login": m.ACTIONS_BOT},
            "body": m.comment_body("ng", ["テストが失敗"], HEAD, "on"),
        }
        self.assertEqual(self.run_main("on", comments=[error, ng]), [])

    def deletes(self, calls):
        return [c for c in calls if c[:3] == ("api", "-X", "DELETE")]

    def test_branch_is_deleted_only_after_successful_merge(self):
        self.assertEqual(len(self.deletes(self.run_main("on"))), 1)
        self.assertEqual(self.deletes(self.run_main("dry-run")), [])
        self.assertEqual(self.deletes(self.run_main("on", merge_error="review required")), [])
        self.assertEqual(self.deletes(self.run_main("on", verdict=("ng", ["x"]))), [])

    def test_branch_delete_checks_fork_and_tip(self):
        tip = subprocess.CompletedProcess([], 0, json.dumps({"object": {"sha": HEAD}}), "")
        moved = subprocess.CompletedProcess([], 0, json.dumps({"object": {"sha": "n" * 40}}), "")
        gone = subprocess.CompletedProcess([], 1, "", "gh: Not Found (HTTP 404)")
        ok = subprocess.CompletedProcess([], 0, "", "")
        failed = subprocess.CompletedProcess([], 1, "", "502")
        cases = [  # (取得の結果, 消す結果, head のリポジトリ, 返り値, 消すか)
            (tip, ok, "o/r", True, True),
            (tip, failed, "o/r", False, True),
            (moved, ok, "o/r", True, False),  # 先頭が変わった（新しい作業）ので残す
            (gone, ok, "o/r", True, False),
            (tip, ok, "someone/r", True, False),  # fork の PR
        ]
        for get, delete, head_repo, expected, deletes in cases:
            calls = []

            def fake(cmd, get=get, delete=delete, calls=calls, **kw):
                calls.append(cmd)
                return delete if "-X" in cmd else get

            with mock.patch("subprocess.run", side_effect=fake):
                self.assertEqual(m.delete_branch("o/r", "issue-3-x", HEAD, head_repo), expected)
            self.assertEqual(any("-X" in c for c in calls), deletes, (head_repo, get.stdout))

    def test_on_ng_comments_without_merging(self):
        calls = self.run_main("on", verdict=("ng", ["テストが失敗"]))
        self.assertEqual([c[:2] for c in calls], [("pr", "comment")])
        self.assertIn("verdict=ng mode=on", calls[0][-1])

    def test_unknown_mode_is_dry_run(self):
        calls = self.run_main("yes")
        self.assertIn("mode=dry-run", calls[0][-1])


class GraphqlTest(unittest.TestCase):
    def test_strings_stay_strings(self):
        done = subprocess.CompletedProcess([], 0, json.dumps({"data": {}}), "")
        with mock.patch("subprocess.run", return_value=done) as run:
            m.gh_graphql("q", owner="o", name="2048", number=7)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("name=2048") - 1], "-f")
        self.assertEqual(cmd[cmd.index("owner=o") - 1], "-f")
        self.assertEqual(cmd[cmd.index("number=7") - 1], "-F")


class ClosingNumbersTest(unittest.TestCase):
    def ref(self, number, repo):
        return {"number": number, "repository": {"nameWithOwner": repo}}

    def test_other_repository_is_not_taken_for_this_one(self):
        self.assertEqual(m.closing_numbers([self.ref(12, "O/R")], "o/r"), [12])
        refs = [self.ref(12, "o/other")]
        self.assertEqual(m.closing_numbers(refs, "o/r"), [-1])
        self.assertIsNone(m.issue_number_of("issue-12-x", m.closing_numbers(refs, "o/r")))
        both = [self.ref(12, "o/r"), self.ref(12, "o/other")]
        self.assertIsNone(m.issue_number_of("issue-12-x", m.closing_numbers(both, "o/r")))


class AfterMergeTest(unittest.TestCase):
    def finish(self, close_error=None, deleted=True):
        with (
            mock.patch.object(
                m,
                "close_issue",
                side_effect=close_error
                and subprocess.CalledProcessError(1, "gh", stderr=close_error),
            ),
            mock.patch.object(m, "delete_branch", return_value=deleted) as delete,
            mock.patch.object(m, "comment") as comment,
        ):
            done = m.finish_merged("o/r", 7, 3, "issue-3-x", HEAD, "on", "o/r")
        return done, delete, comment

    def test_record_is_written_only_after_cleanup(self):
        done, delete, comment = self.finish()
        self.assertTrue(done)
        self.assertIn("verdict=ok mode=on", comment.call_args[0][2])
        self.assertIn(m.CLEANUP_MARKER.format(sha=HEAD), comment.call_args[0][2])
        done, delete, comment = self.finish(close_error="502")
        self.assertFalse(done)
        delete.assert_not_called()
        comment.assert_not_called()
        done, _, comment = self.finish(deleted=False)
        self.assertFalse(done)
        comment.assert_not_called()

    def test_close_issue_removes_labels_even_if_already_closed(self):
        calls = []
        issue = {"state": "closed", "labels": [{"name": "agent:review"}, {"name": "merge:auto"}]}
        with (
            mock.patch.object(m, "gh_api", return_value=issue),
            mock.patch.object(m, "gh", side_effect=lambda *a: calls.append(a)),
        ):
            m.close_issue("o/r", 3)
        self.assertEqual(
            calls, [("issue", "edit", "3", "-R", "o/r", "--remove-label", "agent:review")]
        )

    def retry(
        self,
        merged_by="github-actions[bot]",
        repo_of_ref="o/r",
        merged_at="2026-11-06T00:00:00Z",
        user="agent",
        comments=(),
        events=(),
    ):
        closed = [
            {
                "number": 7,
                "merged_at": merged_at,
                "user": {"login": user},
                "head": {"ref": "issue-3-x", "sha": HEAD, "repo": {"full_name": "o/r"}},
                "base": {"ref": "main"},
            }
        ]

        def api(path, paginate=False):
            if path.startswith("repos/o/r/pulls?state=closed"):
                return closed
            if path == "repos/o/r/issues/7/comments":
                return list(comments)
            if path == "repos/o/r/pulls/7":
                return {"merged_by": {"login": merged_by}}
            if path == "repos/o/r/issues/3/events":
                return list(events)
            raise AssertionError(path)

        refs = {
            "repository": {
                "pullRequest": {
                    "closingIssuesReferences": {
                        "nodes": [{"number": 3, "repository": {"nameWithOwner": repo_of_ref}}]
                    }
                }
            }
        }
        with (
            mock.patch.object(m, "gh_api", side_effect=api),
            mock.patch.object(m, "gh_graphql", return_value=refs),
            mock.patch.object(m, "finish_merged", return_value=True) as finish,
        ):
            m.retry_unfinished("o/r", "agent", "main")
        return finish

    def test_retries_cleanup_without_record(self):
        self.retry().assert_called_once_with("o/r", 7, 3, "issue-3-x", HEAD, "on", "o/r")

    def test_record_from_actions_means_done(self):
        body = m.comment_body("ok", [], HEAD, "on") + "\n" + m.CLEANUP_MARKER.format(sha=HEAD)
        done = {"user": {"login": m.ACTIONS_BOT}, "body": body}
        self.retry(comments=[done]).assert_not_called()
        forged = dict(done, user={"login": "agent"})
        self.retry(comments=[forged]).assert_called_once()

    def test_old_success_comment_is_not_a_record(self):
        # 以前の版は、後片付けの前に「マージしました」と書いていた
        old = {"user": {"login": m.ACTIONS_BOT}, "body": m.comment_body("ok", [], HEAD, "on")}
        self.retry(comments=[old]).assert_called_once()

    def test_issue_reopened_after_merge_is_left_alone(self):
        reopened = [{"event": "reopened", "created_at": "2026-11-06T01:00:00Z"}]
        self.retry(events=reopened).assert_not_called()
        before = [{"event": "reopened", "created_at": "2026-11-05T01:00:00Z"}]
        self.retry(events=before).assert_called_once()

    def test_leaves_others_alone(self):
        for kwargs in [
            {"merged_by": "nosuke223"},
            {"repo_of_ref": "o/other"},
            {"merged_at": None},
            {"user": "someone"},
        ]:
            self.retry(**kwargs).assert_not_called()


if __name__ == "__main__":
    unittest.main()
