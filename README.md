# agent-workflows

[agent-env](https://github.com/nosuke223/agent-env)（非公開）の自律開発で、各プロジェクトの GitHub Actions から呼ぶ共通の部品（アクション）です。ここを直せば、呼んでいるすべてのプロジェクトに効きます。

GitHub では、公開のリポジトリから非公開のリポジトリのワークフローやアクションを呼べないため、共通の部品はこの公開のリポジトリに置いています。中身は、各プロジェクトに写していたものと同じで、秘密情報は含みません。

| 部品 | 中身 | 呼び出し側 |
|---|---|---|
| `auto-merge` | エージェントの PR が条件を満たせば、承認してマージする。`mode` が `on` なら所有者が `merge:auto` を付けた Issue の PR だけ、`all` なら `merge:manual` のないすべての PR（所有者が書いたか、所有者が `agent:ready` を付けた Issue）と、Dependabot の更新の PR（依存のファイルだけを変え、テストが通ったもの）（`auto-merge/auto_merge.py`） | `.github/workflows/auto-merge.yml` |
| `ci/python-cli` | 型 python-cli のテスト（uv、pytest、ruff） | `.github/workflows/ci.yml`（ジョブ名 `test`） |
| `ci/web` | 型 web のテスト（npm の lint、test、build、Playwright の e2e） | `.github/workflows/ci.yml`（ジョブ名 `test`） |

呼び出し側のファイルは、agent-env の `template/` と `template-types/` にあり、プロジェクトを追加するとき（`setup/add_project.py`）に配置されます。

```yaml
# 例: 型 web のテスト
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: nosuke223/agent-workflows/ci/web@main
```

- 呼び出し側は `@main` を使う。このリポジトリの main を変えると、次の実行からすべてのプロジェクトに効く。変更は PR で行い、Codex のレビューとテストを通してからマージする
- 自動マージの方式と条件は、agent-env の `docs/decisions.md`「自動マージの方式」と `docs/review-and-merge.md`「自動マージ」
- テスト: `python3 -m unittest discover -s tests`。lint: `uvx ruff check .`、`uvx ruff format --check .`
