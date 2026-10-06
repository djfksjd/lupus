<div align="center">

<img src="./asset/logo/lupuslogo2.png" alt="LUPUS — AI works together" width="380" />

# LUPUS

**AI WORKS TOGETHER**

### Claude Code と Codex CLI に、検証可能な仕事を最後までやり遂げさせるローカル supervisor —<br/>証拠による完了判定 · 予算 · クラッシュ復旧 · Claude ↔ Codex 引き継ぎ

[English](./README.md) · [한국어](./README.ko.md) · [简体中文](./README.zh-CN.md) · [日本語](./README.ja.md)

[![License](https://img.shields.io/badge/license-Apache--2.0-1f2937?style=flat-square)](./LICENSE)
![Stage](https://img.shields.io/badge/stage-v0.1%20alpha-d69526?style=flat-square)
![Python](https://img.shields.io/badge/python-3.12%2B-3776ab?style=flat-square)
![Dependencies](https://img.shields.io/badge/runtime%20deps-0-2ea043?style=flat-square)
![Tests](https://img.shields.io/badge/offline%20tests-224%20passing-2ea043?style=flat-square)

</div>

Lupus は、すでにインストールしてログイン済みの `claude` と `codex` CLI を（既存のサブスクリプションログインのまま）worker として実行し、「完了」は自分で判定します。モデルが「終わりました」と言ったからではなく、決定的なチェックが通ったときだけ目標は完了になります。目標・予算・試行・チェックポイント・プロジェクト知識をローカルの SQLite に保存するので、クラッシュや利用上限、AI の切り替えの後も作業が続きます。

**現在は `v0.1 alpha` です。** macOS 向けの個人用ツールで、サンドボックスはありません（worker はあなたのユーザー権限で動きます）。以下の測定も小規模です。機密性の高い資料には使わないでください。

## できること

| | |
|---|---|
| **証拠だけで完了** | DONE には、現在の受け入れ条件に結び付いたチェックの合格が必要です。worker の「直しました」は証拠になりません。 |
| **テストの固定** | worker の開始前にテストファイルとランナー設定を固定します。変更・削除・スキップされたテストは検証前に元へ戻します。 |
| **Claude ↔ Codex 引き継ぎ** | 片方の AI が止まったら（上限、クラッシュ、あなたの選択）、もう片方が検証済みチェックポイントから続行します。完了済みの手順はやり直さず、予算と試行回数もリセットされません。 |
| **予算と空回り防止** | 呼び出し・試行・時間・トークンを開始前に予約します。同じ失敗の繰り返しは、モデルを呼ぶ前に拒否します。 |
| **クラッシュに強いチェックポイント** | 復旧用オブジェクトを永続化してから DB にコミットします。すべての境界でプロセスを強制終了するテストで確認済みです。 |
| **軽量起動** | タスクに不要なプラグイン・フック・MCP・スキル説明を読み込まずに起動し、タスクのファイルを prompt に入れます。 |
| **メモリグラフ** | プロジェクト知識を、出所付きの型付きノードとリンクとして保存します。想起は試行に結び付けられ、その試行の検証結果で評価されます。 |
| **失敗テストはコマンド一つ** | `lupus fix-tests` に目標ファイルは不要です。supervisor 自身が失敗を観測し、その失敗が目標になります。 |

## 1 台の Mac での測定（2026-10-05）

同じタスク、同じチェック、実行ごとに新しいディレクトリ。Claude Code 2.1.289、codex-cli 0.160.0。すべての実行がチェックに合格しました。トークン = 新規入力 + キャッシュ入力 + 出力。

| 状況 | 普段の設定のままの CLI | Lupus |
|---|---|---|
| 一度で終わるコーディング 3 種、Claude | 135,729 トークン · 15.0 秒 | 12,166 トークン · 6.2 秒 |
| 一度で終わるコーディング 3 種、Codex | 88,530 トークン · 26.2 秒 | 36,298 トークン · 12.2 秒 |
| 4 ステップのプロジェクト、Claude | 298,327 トークン · 52.0 秒 | 70,768 トークン · 42.5 秒 |
| 同じプロジェクト、中断後に別の AI が引き継ぎ | 337,133 トークン · 82.0 秒 · 再説明 1,139 文字 | 138,989 トークン · 64.1 秒 · 再説明なし |
| 失敗テストの修復、Claude | 136,712 トークン · 16.2 秒 | 12,817 トークン · 5.7 秒 · コマンド一つ |

正直に読んでください。

- **節約の大部分は、不要な設定を読み込まないことと prompt の工夫（ファイル同梱、一度で書く）によるものです。** 同じ工夫を Lupus なしで使った CLI 呼び出しは、一度で終わるタスクで同じトークンを使いました。その場合に supervisor が加えるのは安いトークンではなく、検証された完了です。
- チェック付きの目標を定義するには、普通の prompt の約 1.9 倍の入力が必要です（`fix-tests` を除く）。
- 引き継ぎシナリオの Codex 区間は、通常の CLI より 33% **多く** 使いました。Lupus はステップごとに呼び出し、Codex は呼び出しごとの固定入力が大きいためです。
- 各セル 1〜2 回の実行です。隠した holdout テスト付きのより難しい 3 タスクでは 36 回すべて合格し、誤った合格はありませんでした。そのため、テスト固定が偽の完了を減らすことはこのベンチマークでは示せていません。

生データとスクリプト：[`lupus/docs/`](./lupus/docs) · [`lupus/evaluations/`](./lupus/evaluations) · 全記録は [docs/design/LUPUS-IMPLEMENTATION-REVIEW.md](./docs/design/LUPUS-IMPLEMENTATION-REVIEW.md)（韓国語）。

## 使うとき、使わないとき

**向いている場合**：プログラムで確認できる仕事。失敗しているテスト、テストのある複数ステップの作業、中断や Claude と Codex の切り替えがあり得る長い作業、一度書いておく価値のある規約があるプロジェクト。

**普通の CLI のほうがよい場合**：単発の質問、対話しながらの探索、プラグインや MCP が必要な作業、プログラムで判定できない仕事（企画、調査、文章、デザイン）。

## クイックスタート

必要環境：macOS、SQLite が **3.51.3 以上で FTS5 有効** な Python **3.12+**（Homebrew の Python で可）、ログイン済みの `claude` または `codex`。

```bash
git clone https://github.com/djfksjd/lupus.git
cd lupus/lupus
python3 -m pip install -e .          # または export PYTHONPATH=src して `python3 -m lupus`

lupus init                           # ~/.lupus を作成（DB + vault）
lupus probe --live                   # インストール済み CLI が実際に対応する範囲を測定（小さな呼び出し 2 回）

cd ~/work/my-project                 # 失敗しているテストがあるプロジェクト
lupus fix-tests --driver claude      # 失敗を観測 -> テスト固定 -> 修正 -> 検証
lupus do "エクスポートコマンドに --json オプションを追加" --driver claude
                                     # 任意の依頼：失敗するテストを先に作成 -> 読んで承認 -> 実装
```

自分でチェックを決めた目標：

```bash
lupus project-add ~/work/site --name site --providers anthropic,openai
lupus goal-submit <project_id> goal.json
lupus run <goal_id> --driver claude
lupus status <goal_id>               # 状態、予算、再開できるか、できない理由
lupus run <goal_id> --driver codex   # 別の AI で続行（検証済みの引き継ぎ）
lupus graph --open                   # 知識グラフ画面
```

`goal.json` の形式と全コマンド：[`lupus/README.md`](./lupus/README.md)（韓国語）。

## 仕組み

```text
 you ──► goal + acceptance checks ──► ┌──────────────── Lupus supervisor (plain program) ───────────────┐
                                      │ budget · attempts · leases · checkpoints · frozen tests · memory │
                                      └───────┬───────────────────────────────────────────────┬─────────┘
                                   lean prompt│                                               │verify (deterministic)
                                              ▼                                               ▼
                                   claude  ◄──handoff──►  codex                    PASS evidence ──► DONE
```

- supervisor は普通のプログラムです。割り当て・予算・再試行・完了をモデル呼び出しで決めません。
- worker が受け取るのは prompt とディレクトリだけです。DB も CLI も権限も渡しません。
- グローバル設定は変更しません。フック、PATH、`~/.claude`、`~/.codex` には触れません。

<div align="center">
<img src="./asset/screenshots/graph.png" alt="Lupus 知識グラフ画面" width="760" />
<br/><sub>知識グラフ画面（<code>lupus graph</code>）：ノード、型付きリンク、知識が記録・想起された目標</sub>
</div>

## 知っておくべき制限

- **VM やコンテナによる隔離はありません。** 検証器は macOS サンドボックス内で動き、worker は各 CLI の制限機能で閉じ込めますが、CLI 自体はあなたとして動きます。保護すべきデータや顧客データを渡さないでください。読み取り範囲は各 CLI が許す限り閉じ込め、canary ファイルで実測します。Codex はホームディレクトリを読めず、コマンドがネットワークを使えない権限プロファイルで起動します。閉じ込めが確認できない driver にはプロジェクトごとの明示的な同意が必要です。[SECURITY.md](./SECURITY.md) を参照。
- 普段の `claude` / `codex` セッションとは連携しません。`lupus` を明示的に実行します。対話モードはありません。
- `fix-tests` は Python の `unittest`/`pytest` プロジェクトのみ対応です。
- まだ若いコードです。8 回の外部レビューで 60 件の欠陥を見つけて修正しました。まだ残っていると考えてください。
- サブスクリプション枠の実際の消費量は観測できません。トークン数は CLI が報告した値です。

## ドキュメント

- [マシン契約](./lupus/docs/CONTRACT.md) — コードが強制するルールと対象外の項目（韓国語）
- [実装記録](./docs/design/LUPUS-IMPLEMENTATION-REVIEW.md) — 決定、レビュー、すべての測定とその限界（韓国語）
- [設計](./docs/design/LUPUS-PLAN.md) — 全体設計（韓国語）
- [サードパーティ表記](./lupus/THIRD_PARTY_NOTICES.md) — グラフ画面に vis-network を無改変で同梱

## ライセンス

[Apache-2.0](./LICENSE)。同梱の vis-network は MIT で使用しています。
