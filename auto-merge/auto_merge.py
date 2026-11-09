"""エージェントの PR が、自動マージの条件を満たしているかを判定する（GitHub Actions で動かす）。

条件は agent-env の docs/decisions.md「自動マージの方式」のとおり。
判定に使うのは GitHub 上の記録だけで、エージェントが書いた印（PR のラベルなど）は信じない。

環境変数:
    GH_TOKEN       GitHub Actions のトークン
    REPO           所有者/リポジトリ
    AGENT_ACCOUNT  エージェントの GitHub アカウント
    AUTO_MERGE     off: 何もしない / dry-run（未設定も同じ）: 判定の結果をコメントするだけ /
                   on: 条件を満たしたら承認してマージする

標準ライブラリと gh だけで動く。nosuke223/agent-workflows の auto-merge アクションから動かす
（各プロジェクトは、このアクションを呼ぶだけ）。テストは tests/test_auto_merge.py。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys

CODEX_BOT = "chatgpt-codex-connector[bot]"
ACTIONS_BOT = "github-actions[bot]"
CODEX_STATE_RE = re.compile(r"<!-- codex-review-state:v1 (\{.*?\}) -->", re.DOTALL)
AUTO_LABEL = "merge:auto"
# AUTO_MERGE=all のとき、これが付いた Issue の PR は自動マージしない（人が見る）
MANUAL_LABEL = "merge:manual"
READY_LABEL = "agent:ready"
TEST_CHECK = "test"
# 判定のコメントの印。result は判定の結果と理由の要約で、変わったときだけコメントし直す。
# verdict（ok / ng / error）と mode（dry-run / on）は、ランナーが通知を決めるのに読む
MARKER = "<!-- auto-merge-check sha={sha} result={result} verdict={verdict} mode={mode} -->"
MARKER_RE = re.compile(
    r"<!-- auto-merge-check sha=(\w+) result=(\w+)(?: verdict=([\w-]+) mode=([\w-]+))? -->"
)
# 後片付け（Issue を閉じる、ラベル、ブランチ）が済んだ記録。
# 済んでから書く「マージしました」のコメントに付ける。
# 以前の版は後片付けの前に「マージしました」と書いていたので、判定の印とは別にする
CLEANUP_MARKER = "<!-- auto-merge-cleanup-done sha={sha} -->"
CLEANUP_RE = re.compile(r"<!-- auto-merge-cleanup-done sha=(\w+) -->")
# 変えていたら人が見る（ルール、設定、CI、依存）
# 依存のファイルは、型（python-cli、web）のものをまとめて見る
CAREFUL_NAMES = {
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    ".npmrc",
    "CLAUDE.md",
    "AGENTS.md",
}


def gh_api(path: str, paginate: bool = False):
    cmd = ["gh", "api", path] + (["--paginate", "--slurp"] if paginate else [])
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    if paginate:
        return [item for page in data for item in page]
    return data


def gh_graphql(query: str, **variables) -> dict:
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        # 文字は -f（そのまま文字）、整数だけ -F（-F は "2048" や "true" を数や真偽に変える）
        flag = "-F" if isinstance(value, int) and not isinstance(value, bool) else "-f"
        cmd += [flag, f"{key}={value}"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return json.loads(out)["data"]


# 判定（GitHub に触れない部分）


def issue_number_of(branch: str, closing: list[int]) -> int | None:
    """ブランチ名 issue-<N>-... と、PR が閉じる Issue が同じ1件を指していれば、その番号。"""
    m = re.match(r"issue-(\d+)-", branch)
    if not m or closing != [int(m.group(1))]:
        return None
    return int(m.group(1))


def closing_numbers(refs: list, repo: str) -> list[int]:
    """PR が閉じる Issue の番号。別のリポジトリの Issue は -1 にする。

    番号だけで、このリポジトリの Issue と取り違えないようにするため。

    -1 が混ざると issue_number_of が「1件だけ」を満たさないので、その PR は対象外になる。
    """
    numbers = []
    for ref in refs:
        name = ((ref.get("repository") or {}).get("nameWithOwner") or "").lower()
        numbers.append(ref["number"] if name == repo.lower() else -1)
    return numbers


def label_added_by(events: list, label: str) -> str | None:
    """ラベルを最後に付けたアカウント。今付いているかは呼び出し側で確かめる。"""
    actor = None
    for e in events:
        if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label:
            actor = (e.get("actor") or {}).get("login")
    return actor


# 「判断してほしいこと」への答えだけのコメント（例: 「1:A, 2:B」「おすすめで」）。
# 選択肢は本文にあり、
# Codex も本文で読めるので、自動マージの対象から外す理由にしない
# 質問が1つのときの「A」「Aで」も答えとして扱う（全角の英字も）
CHOICE = r"[A-Za-zＡ-Ｚａ-ｚ]"
ANSWER_RE = re.compile(
    rf"^\s*(おすすめ|{CHOICE}|(\d+\s*[:：]\s*{CHOICE}\s*[,、，]?\s*)+)"
    r"\s*(で(お願いします)?)?[。.]?\s*$"
)


def counts_as_spec(body: str) -> bool:
    """所有者の Issue へのコメントのうち、仕様を足しうるもの（答えだけのものは除く）。"""
    return not ANSWER_RE.match(body or "")


def in_scope(labels: set, scope_all: bool) -> bool:
    """自動マージの対象か。

    all なら merge:manual のないものすべて、そうでなければ merge:auto のもの。
    """
    if MANUAL_LABEL in labels:
        return False
    return scope_all or AUTO_LABEL in labels


def owner_approval(issue: dict, events: list, owner: str, scope_all: bool) -> str | None:
    """所有者が自動マージを認めていなければ、その理由。認めていれば None。

    all でないときは、所有者が merge:auto を付けたことだけを見る。all のときは、Issue を書いたのが
    所有者か、所有者が agent:ready を付けた（リードの下書きを承認した）ことでもよい。
    """
    actor = label_added_by(events, AUTO_LABEL)
    if actor == owner:
        return None
    if not scope_all:
        return f"`{AUTO_LABEL}` を付けたのが所有者ではない（{actor}）"
    if (issue.get("user") or {}).get("login") == owner:
        return None
    if any(
        e.get("event") == "labeled"
        and (e.get("label") or {}).get("name") == READY_LABEL
        and (e.get("actor") or {}).get("login") == owner
        for e in events
    ):
        return None
    return f"所有者が書いた Issue でも、所有者が `{READY_LABEL}` を付けた Issue でもない"


def codex_state(comments: list) -> dict | None:
    """Codex のまとめのコメントに埋め込まれた状態のうち、最新のもの。"""
    latest = None
    for c in comments:
        if (c.get("user") or {}).get("login") != CODEX_BOT:
            continue
        m = CODEX_STATE_RE.search(c.get("body") or "")
        if not m:
            continue
        try:
            review = json.loads(m.group(1)).get("code_review") or {}
        except ValueError:
            continue
        if review.get("commit_sha"):
            latest = review
    return latest


def codex_clean(state: dict | None, head: str) -> bool | None:
    """True: 最新のコミットに指摘なし / False: 指摘あり / None: まだ（レビュー中など）。

    完了していても has_any_findings が空（null）のことがある（レビューの報告がなく、
    実際にレビューされたか分からない）。指摘なしと明記されたときだけ True にする。
    """
    if not state or state.get("commit_sha") != head or state.get("status") != "completed":
        return None
    findings = state.get("has_any_findings")
    if findings is None:
        return None
    return not findings


def codex_receipt_clean(comments: list, head: str) -> bool:
    """Codex の返事のコメントに、head のコミットで「大きな問題なし」とあるか。

    まれに、まとめの状態の has_any_findings が空（null）のままになる。そのとき `@codex review` で
    頼み直すと、指摘がなければ「Didn't find any major issues」のコメントで返ってくる
    （issue-digest PR #163）。
    """
    for c in comments:
        body = c.get("body") or ""
        if (
            (c.get("user") or {}).get("login") == CODEX_BOT
            and "codex-review-receipt:v1" in body
            and "Didn't find any major issues" in body
            and f"`{head[:10]}" in body
        ):
            return True
    return False


def codex_result(comments: list, head: str) -> bool | None:
    """codex_clean と同じ値。状態が空でも、返事のコメントで指摘なしと分かれば True。"""
    clean = codex_clean(codex_state(comments), head)
    if clean is None and codex_receipt_clean(comments, head):
        return True
    return clean


def test_result(check_runs: list) -> bool | None:
    """True: test が成功 / False: 失敗 / None: まだ（ないか、実行中）。"""
    runs = [r for r in check_runs if r.get("name") == TEST_CHECK]
    if not runs or any(r.get("status") != "completed" for r in runs):
        return None
    return all(r.get("conclusion") == "success" for r in runs)


def changed_paths(files: list) -> list[str]:
    """PR の変更したファイルのパス。名前を変えたファイルは、変える前のパスも含める
    （ルールのファイルを別の場所へ移すと、移した先のパスだけでは見落とすため）。"""
    paths = []
    for f in files:
        paths.append(f["filename"])
        if f.get("previous_filename"):
            paths.append(f["previous_filename"])
    return paths


def is_careful(path: str) -> bool:
    """ルール、設定、CI、依存のファイルか。下の階層にあるものも含める。

    下の階層の CLAUDE.md や .claude/ もエージェントのルールとして読まれ、
    package.json なども依存になるため。
    """
    parts = path.split("/")
    name = parts[-1]
    return (
        path.startswith(".github/")
        or ".claude" in parts[:-1]
        or name in CAREFUL_NAMES
        or name.startswith(".env")
    )


def careful_files(files: list[str]) -> list[str]:
    return [f for f in files if is_careful(f)]


def judge(facts: dict) -> tuple[str, list[str]]:
    """facts から、("wait" | "ok" | "ng", 満たしていない条件) を返す。

    wait は、まだ決められない（Codex のレビューかテストが終わっていない、Codex の指摘があり
    ランナーが直させる途中、衝突の判定中）。コメントせずに次の回を待つ。
    """
    if facts["codex"] is not True or facts["test"] is None or facts["mergeable"] is None:
        return "wait", []
    reasons = []
    if facts["unapproved"]:
        reasons.append(facts["unapproved"])
    if facts["owner_comments"]:
        reasons.append(
            f"Issue に所有者のコメントが {facts['owner_comments']} 件ある"
            "（Codex はコメントの仕様を読めない）"
        )
    if facts["test"] is False:
        reasons.append("GitHub 上のテストか lint が失敗している")
    if facts["careful"]:
        reasons.append("ルールや設定のファイルを変えている: " + ", ".join(facts["careful"]))
    if facts["mergeable"] is False:
        reasons.append("main と衝突している")
    return ("ng" if reasons else "ok"), reasons


def result_digest(verdict: str, reasons: list[str]) -> str:
    return hashlib.sha256("\n".join([verdict, *reasons]).encode()).hexdigest()[:12]


def comment_body(verdict: str, reasons: list[str], sha: str, mode: str = "dry-run") -> str:
    """verdict は ok / ng / error（条件は満たしたが、承認かマージに失敗した）。"""
    listed = "\n\n" + "\n".join(f"- {r}" for r in reasons) if reasons else ""
    if mode != "on":
        if verdict == "ok":
            text = "🤖 自動マージの判定（試運転）: 条件を満たしています。本番ならマージします。"
        else:
            text = "🤖 自動マージの判定（試運転）: 条件を満たしていません。" + listed
    elif verdict == "ok":
        text = "🤖 自動マージ: 条件を満たしたので、承認してマージしました。"
    elif verdict == "error":
        text = "🤖 自動マージ: 条件は満たしましたが、マージできませんでした。" + listed
    else:
        text = "🤖 自動マージ: 条件を満たしていないので、マージしません。" + listed
    marker = MARKER.format(
        sha=sha, result=result_digest(verdict, reasons), verdict=verdict, mode=mode
    )
    return f"{text}\n\n対象のコミット: `{sha[:7]}`\n{marker}"


# GitHub から事実を集める

CLOSING_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      closingIssuesReferences(first: 10) { nodes { number repository { nameWithOwner } } }
    }
  }
}
"""


def collect(
    repo: str, owner: str, pr: dict, scope_all: bool = False
) -> tuple[int | None, dict | None]:
    """PR の Issue 番号と、判定に使う事実。対象外なら (None, None)。"""
    name = repo.split("/", 1)[1]
    data = gh_graphql(CLOSING_QUERY, owner=owner, name=name, number=pr["number"])
    refs = data["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"]
    number = issue_number_of(pr["head"]["ref"], closing_numbers(refs, repo))
    if number is None:
        return None, None
    issue = gh_api(f"repos/{repo}/issues/{number}")
    if not in_scope({label["name"] for label in issue.get("labels", [])}, scope_all):
        return number, None
    head = pr["head"]["sha"]
    detail = gh_api(f"repos/{repo}/pulls/{pr['number']}")
    events = gh_api(f"repos/{repo}/issues/{number}/events", paginate=True)
    issue_comments = gh_api(f"repos/{repo}/issues/{number}/comments", paginate=True)
    pr_comments = gh_api(f"repos/{repo}/issues/{pr['number']}/comments", paginate=True)
    checks = gh_api(f"repos/{repo}/commits/{head}/check-runs?per_page=100")
    files = gh_api(f"repos/{repo}/pulls/{pr['number']}/files", paginate=True)
    facts = {
        "owner": owner,
        "unapproved": owner_approval(issue, events, owner, scope_all),
        "owner_comments": sum(
            1 for c in issue_comments if c["user"]["login"] == owner and counts_as_spec(c["body"])
        ),
        "codex": codex_result(pr_comments, head),
        "test": test_result(checks.get("check_runs", [])),
        "careful": careful_files(changed_paths(files)),
        "mergeable": detail.get("mergeable"),
        "pr_comments": pr_comments,
    }
    return number, facts


def markers(comments: list):
    """GitHub Actions（このワークフロー）が書いた判定のコメントの印。

    ほかのアカウントが印を書いても、判定やマージの記録として扱わない（偽の印で止めたり、飛ばしたりできないように）。
    """
    for c in comments:
        if (c.get("user") or {}).get("login") != ACTIONS_BOT:
            continue
        yield from MARKER_RE.finditer(c.get("body") or "")


def last_marker(comments: list, sha: str) -> re.Match | None:
    """このコミットへの最後の判定のコメントの印。"""
    last = None
    for m in markers(comments):
        if m.group(1) == sha:
            last = m
    return last


def already_commented(
    comments: list, sha: str, verdict: str, reasons: list[str], mode: str = "dry-run"
) -> bool:
    """このコミットへの最後の判定のコメントが、同じ結果と理由と動きか。

    同じコミットでも、テストの再実行や所有者のコメントで結果が変わることがあるので、
    結果か理由が変わったらコメントし直す。試運転から本番に切り替えたときも、本番の処理をする。
    """
    last = last_marker(comments, sha)
    if not last or last.group(2) != result_digest(verdict, reasons):
        return False
    return (last.group(4) or "dry-run") == mode


def merge_failed(comments: list, sha: str) -> bool:
    """このコミットで、前にマージに失敗したか。失敗は人が見るので、同じコミットでは繰り返さない。

    最後の判定だけでなく、そのコミットへのすべての判定を見る（失敗の後に別の判定が出ることがあるため）。
    """
    return any(m.group(1) == sha and m.group(3) == "error" for m in markers(comments))


def gh(*args: str) -> None:
    subprocess.run(["gh", *args], capture_output=True, text=True, check=True)


def approve_and_merge(repo: str, pr: int, sha: str) -> None:
    """承認してマージする。判定したコミットのままのときだけマージする。

    直前に push されていたら、マージは失敗する。
    """
    gh(
        "pr",
        "review",
        str(pr),
        "-R",
        repo,
        "--approve",
        "--body",
        "自動マージの条件を満たしたため、承認します。",
    )
    gh("pr", "merge", str(pr), "-R", repo, "--merge", "--match-head-commit", sha)


def delete_branch(repo: str, branch: str, sha: str, head_repo: str | None) -> bool:
    """マージした PR のブランチを消す。消すべきものが残っていなければ True。

    Actions のトークンでマージすると、リポジトリの「マージ後にブランチを消す」設定が働かないため。
    消すのは、ブランチがこのリポジトリのもので（fork ではない）、
    先頭がマージしたコミットのままのときだけ。
    同じ名前で作り直されたブランチや、新しいコミットが足されたブランチは、未マージの作業なので残す。
    """
    if (head_repo or "").lower() != repo.lower():
        return True  # fork の PR。マージ先の同じ名前のブランチは別物
    ref = f"repos/{repo}/git/refs/heads/{branch}"
    current = subprocess.run(["gh", "api", ref], capture_output=True, text=True)
    if current.returncode != 0:
        return "Not Found" in (current.stderr or "")  # すでに消えている
    if json.loads(current.stdout).get("object", {}).get("sha") != sha:
        return True
    result = subprocess.run(["gh", "api", "-X", "DELETE", ref], capture_output=True, text=True)
    return result.returncode == 0 or "Reference does not exist" in (result.stderr or "")


def close_issue(repo: str, issue: int) -> None:
    """Issue を「完了」で閉じ、agent:* のラベルを外す。閉じていれば、ラベルだけ外す。

    Actions のトークンでマージすると、PR の `Closes` があっても Issue は閉じない。
    閉じたことも別の Actions に届かず、いつもラベルを外す「エージェントのラベルを外す」も動かない。
    そのため、ここで両方を行う。「完了」で閉じるのは、ランナーが着手の条件を「済み」と読むため。
    """
    data = gh_api(f"repos/{repo}/issues/{issue}")
    if data.get("state") == "open":
        gh("issue", "close", str(issue), "-R", repo, "--reason", "completed")
    for label in data.get("labels", []):
        if label["name"].startswith("agent:"):
            gh("issue", "edit", str(issue), "-R", repo, "--remove-label", label["name"])


def main() -> int:
    mode = os.environ.get("AUTO_MERGE") or "dry-run"
    if mode == "off":
        print("AUTO_MERGE=off のため、何もしません")
        return 0
    # all: merge:manual のないすべての PR を対象にする（動きは on と同じ。印には on と書く）
    scope_all = mode == "all"
    if scope_all:
        mode = "on"
    if mode != "on":
        mode = "dry-run"
    repo = os.environ["REPO"]
    owner = repo.split("/", 1)[0]
    agent = os.environ["AGENT_ACCOUNT"]
    default_branch = gh_api(f"repos/{repo}")["default_branch"]
    for pr in gh_api(f"repos/{repo}/pulls?state=open&per_page=100"):
        if pr["user"]["login"] != agent or pr.get("draft"):
            continue
        if (pr.get("base") or {}).get("ref") != default_branch:
            # main 以外へのマージで Issue を閉じると、main に入っていないのに完了扱いになる
            print(f"PR #{pr['number']}: マージ先が {default_branch} ではないので対象外")
            continue
        number, facts = collect(repo, owner, pr, scope_all)
        if facts is None:
            continue
        verdict, reasons = judge(facts)
        sha = pr["head"]["sha"]
        comments = facts["pr_comments"]
        print(f"PR #{pr['number']}（Issue #{number}）: {verdict} {reasons}")
        if verdict == "wait" or already_commented(comments, sha, verdict, reasons, mode):
            continue
        if mode == "on" and verdict == "ok":
            if merge_failed(comments, sha):
                continue
            try:
                approve_and_merge(repo, pr["number"], sha)
            except subprocess.CalledProcessError as e:
                error = (e.stderr or "").strip() or f"終了コード {e.returncode}"
                print(f"PR #{pr['number']}: マージに失敗しました: {error}")
                comment(repo, pr["number"], comment_body("error", [error], sha, mode))
                continue
            if not gh_api(f"repos/{repo}/pulls/{pr['number']}").get("merged"):
                # マージキューなどで、マージのコマンドが成功してもまだマージされていない。
                # 後片付けは、マージされた後の回の retry_unfinished が行う
                print(f"PR #{pr['number']}: まだマージされていないので、後片付けは後の回で行う")
                continue
            head_repo = (pr["head"].get("repo") or {}).get("full_name")
            finish_merged(repo, pr["number"], number, pr["head"]["ref"], sha, mode, head_repo)
            continue
        comment(repo, pr["number"], comment_body(verdict, reasons, sha, mode))
    if mode == "on":
        retry_unfinished(repo, agent, default_branch)
    return 0


def finish_merged(
    repo: str, pr: int, issue: int, branch: str, sha: str, mode: str, head_repo: str | None
) -> bool:
    """マージの後片付け。Issue を閉じてラベルを外し、ブランチを消し、済んだらコメントを書く。

    そのコメント（判定の印 verdict=ok mode=on）が、後片付けが済んだ記録になる。
    どれかが失敗したら書かず、次の回の retry_unfinished がやり直す。済んだら True。
    """
    try:
        close_issue(repo, issue)
    except subprocess.CalledProcessError as e:
        print(f"PR #{pr}: Issue を閉じられませんでした（次の回にやり直す）: {e.stderr}")
        return False
    if not delete_branch(repo, branch, sha, head_repo):
        print(f"PR #{pr}: ブランチを消せませんでした（次の回にやり直す）")
        return False
    comment(repo, pr, comment_body("ok", [], sha, mode) + "\n" + CLEANUP_MARKER.format(sha=sha))
    return True


def cleanup_done(comments: list, sha: str) -> bool:
    """この PR の後片付けが済んだ記録（GitHub Actions のコメントの CLEANUP_MARKER）があるか。"""
    for c in comments:
        if (c.get("user") or {}).get("login") != ACTIONS_BOT:
            continue
        if any(m.group(1) == sha for m in CLEANUP_RE.finditer(c.get("body") or "")):
            return True
    return False


def reopened_after(events: list, when: str) -> bool:
    """when（マージした時刻）より後に、Issue が開き直されたか。

    人の判断なので、後片付けで閉じ直さない。
    """
    return any(e.get("event") == "reopened" and (e.get("created_at") or "") > when for e in events)


RETRY_DAYS = 14  # 後片付けをやり直す対象: この日数のうちに更新された、閉じた PR


def recently_closed_prs(repo: str, now: dt.datetime) -> list:
    """RETRY_DAYS 日のうちに更新された、閉じた PR（新しい順）。ページをまたいで読む。

    件数で区切ると、ほかの PR がたくさん閉じたときに、後片付けが残った PR を見落とすため、
    日数で区切る。
    """
    cutoff = (now - dt.timedelta(days=RETRY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    prs, page = [], 1
    while True:
        batch = gh_api(
            f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=50&page={page}"
        )
        recent = [pr for pr in batch if (pr.get("updated_at") or "") >= cutoff]
        prs += recent
        if len(recent) < len(batch) or len(batch) < 50:
            return prs
        page += 1


def retry_unfinished(repo: str, agent: str, default_branch: str) -> None:
    """GitHub Actions がマージしたのに、後片付けが済んだ記録のない PR の後片付けをやり直す。

    マージした回の後片付けが一時的なエラーで失敗すると、PR はもう開いていないので、
    判定の流れでは二度と見ない。最近閉じた PR から探す。マージの後に Issue が開き直されていたら、
    所有者の判断なので触らない。
    """
    recent = recently_closed_prs(repo, dt.datetime.now(dt.timezone.utc))
    for pr in recent:
        if not pr.get("merged_at") or pr["user"]["login"] != agent:
            continue
        if (pr.get("base") or {}).get("ref") != default_branch:
            continue
        if not re.match(r"issue-\d+-", pr["head"]["ref"]):
            continue
        sha = pr["head"]["sha"]
        comments = gh_api(f"repos/{repo}/issues/{pr['number']}/comments", paginate=True)
        if cleanup_done(comments, sha):
            continue
        detail = gh_api(f"repos/{repo}/pulls/{pr['number']}")
        if (detail.get("merged_by") or {}).get("login") != ACTIONS_BOT:
            continue  # 人がマージしたものは、人が判断する
        owner, name = repo.split("/", 1)
        data = gh_graphql(CLOSING_QUERY, owner=owner, name=name, number=pr["number"])
        refs = data["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"]
        number = issue_number_of(pr["head"]["ref"], closing_numbers(refs, repo))
        if number is None:
            continue
        events = gh_api(f"repos/{repo}/issues/{number}/events", paginate=True)
        if reopened_after(events, pr["merged_at"]):
            print(f"PR #{pr['number']}: マージの後に Issue #{number} が開き直されたので、触らない")
            continue
        print(f"PR #{pr['number']}: 後片付けが済んでいないので、やり直す")
        head_repo = (pr["head"].get("repo") or {}).get("full_name")
        finish_merged(repo, pr["number"], number, pr["head"]["ref"], sha, "on", head_repo)


def comment(repo: str, pr: int, body: str) -> None:
    gh("pr", "comment", str(pr), "-R", repo, "--body", body)


if __name__ == "__main__":
    sys.exit(main())
