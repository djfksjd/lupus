<div align="center">

<img src="./asset/logo/lupuslogo2.png" alt="LUPUS — AI works together" width="380" />

# LUPUS

**AI WORKS TOGETHER**

### Claude Code と Codex CLI に、検証可能な仕事を最後までやり遂げさせるローカル supervisor —<br/>証拠による完了判定 · 予算 · クラッシュ復旧 · Claude ↔ Codex 引き継ぎ

[English](./README.md) · [한국어](./README.ko.md) · [简体中文](./README.zh-CN.md) · [日本語](./README.ja.md)

[![License](https://img.shields.io/badge/license-Apache--2.0-1f2937?style=flat-square)](./LICENSE)
![Stage](https://img.shields.io/badge/stage-v0.2%20alpha-d69526?style=flat-square)
![Python](https://img.shields.io/badge/python-3.12%2B-3776ab?style=flat-square)
![Dependencies](https://img.shields.io/badge/runtime%20deps-0-2ea043?style=flat-square)
![Tests](https://img.shields.io/badge/offline%20tests-318%20passing-2ea043?style=flat-square)

</div>

Lupus は、すでにインストールしてログイン済みの `claude` と `codex` CLI を（既存のサブスクリプションログインのまま）worker として実行し、「完了」は自分で判定します。worker が手を出せないチェックが通ったときだけ完了になり、モデルが「終わりました」と言ったことは理由になりません。

**現在は `v0.2 alpha` です。** macOS 向けの個人用ツールです。worker と検証器は OS のサンドボックス内で動きますが、VM ではありません。以下の測定は小規模です。機密性のある資料には使わないでください。

## できること

| | |
|---|---|
| **証拠だけで完了** | DONE には、現在の受け入れ条件に結び付いたチェックの合格が必要です。worker の「直しました」は証拠になりません。 |
| **テストの凍結** | worker の開始前にテストファイルとランナー設定を凍結します。編集・削除・スキップされたテストは検証の前に元に戻します。 |
| **Python、Node、Go、Rust、または任意のコマンド** | unittest、pytest、node:test、jest、vitest、`go test`、`cargo test` をプロジェクトのファイルだけから判別します。それ以外は `--check "<テストコマンド>"`。 |
| **どんな依頼も 1 行で** | `lupus fix-tests` は自分で観測した失敗を目標にします。`lupus do "<依頼>"` は 1 回の呼び出しで失敗するテストと（別に保管する）実装案を作り、テストの承認後に実装案を適用して検証します。 |
| **文書・計画・調査** | `lupus write`：まず判定基準をあなたが承認し、書き手とは別の AI が、認める項目ごとに文書を引用しながら判定し、最後にその版をあなたが承認します。 |
| **普段の対話セッション** | `lupus session` は、普段の `claude` / `codex` の画面をあなたの設定のまま起動し、テストを凍結し、終了時に自分で検証します。 |
| **Claude ↔ Codex の引き継ぎ** | 片方が止まると（上限、中断、あなたの選択）、もう片方が検証済みの checkpoint から続けます。終わったステップはやり直さず、予算や試行回数もリセットされません。 |
| **複数プロジェクトをバックグラウンドで** | `lupus alpha-run` は、未完了の目標すべてを共有予算のもとで順番に進め、ある AI の上限が尽きたら別の AI に切り替えます。`--background` を付けると端末を閉じても動き続けます。 |
| **予算とループ制御** | 呼び出し・試行・時間・トークンを開始前に予約します。同じ失敗の繰り返しはモデルを呼ぶ前に拒否します。 |
| **クラッシュに強い checkpoint** | 復旧用のデータを DB のコミットより先に永続化します。すべての境界でプロセスを強制終了して試験しました。 |
| **作業ツリーはそのまま** | `--isolated` は、コミット済み HEAD の別チェックアウトで作業します。`lupus diff` で結果を確認し、`lupus accept` で 1 つのコミットとして取り込み（fast-forward のみ、そのコミット自体を再検査）、`lupus discard` で破棄します。 |
| **OS レベルの閉じ込め** | Claude の worker とすべての検証器は、Lupus が適用する macOS サンドボックス内で動きます。検証器は Docker コンテナで動かすこともできます。 |
| **居場所を自分で証明する記憶** | プロジェクトの知識を、型とリンクを持つノードとして保持します。`lupus learn` は記録された失敗から手順を提案し、その後の検証結果によって昇格または引退するまで候補のままです。 |

## 1 台の Mac での測定（2026-10-06）

同じタスク、同じチェック、実行ごとに新しいディレクトリ、各セル 3 回、すべて合格。Claude Code 2.1.290、codex-cli 0.160.0。トークン = 新規入力 + キャッシュ入力 + 出力で、3 タスクの合計の 3 回平均です。

| 単発のコーディングタスク 3 種 | 普段の設定の CLI | 同じ CLI、プラグイン/MCP なし | Lupus |
|---|---|---|---|
| Claude | 400,640 トークン · 53.9 s | 55,290 · 33.5 s | **37,216 · 18.0 s** |
| Codex | 244,458 トークン · 58.7 s | 203,780 · 50.6 s | **107,931 · 28.5 s** |

| その他の状況 | Lupus なし | Lupus |
|---|---|---|
| 4 ステップのプロジェクト、Claude（1〜2 回、2026-10-05） | 298,327 トークン · 52.0 s | 70,768 · 42.5 s |
| 同じプロジェクトを中断し、別の AI が引き継ぐ（2026-10-05） | 337,133 トークン · 82.0 s · 再説明 1,139 文字 | 138,989 · 64.1 s · 不要 |
| 4 ステップのプロジェクト、Codex、ステップごとの呼び出し 対 まとめて呼び出し | 185,278 トークン · 89.0 s | 122,595 · 53.1 s |
| `lupus do` による機能依頼 対 普通の 1 回の呼び出し、Claude（3 回、2026-10-07） | 17,493 トークン · 10.1 s | **15,629 · 15.1 s** |
| 同じ比較、Codex（3 回、2026-10-07） | 72,083 トークン · 24.4 s | **37,502 · 24.8 s** |

**実際のプロジェクトで。** [hukkin/tomli](https://github.com/hukkin/tomli) のメンテナーが実際に行った 3 件の変更（バグ修正、TOML 1.1 の機能、堅牢化）について、ソースはそのコミットの直前、テストは直後の状態にし、ほかには何も与えませんでした。`lupus fix-tests` は Claude（36k〜64k トークン、10〜21 秒）でも Codex（82k〜119k トークン、16〜21 秒）でも 3 件すべてを最初の試行で完了しました。判定は upstream のテストで行い、どの実行もそのテストを編集しようとはしませんでした。

**より多くの依頼を正しくこなせるのか。小規模な試行の答えは「いいえ」です。** 5 つのプロジェクト（sqlparse、more-itertools、tomli、packaging、click）の実際の upstream コミット 20 件を使いました。worker に渡したのはコミット直前のリポジトリとコミットメッセージだけで、upstream のテストは隠しておき採点に使いました。`lupus do` が下書きしたチェックは自動で承認しました。これは本来の使い方ではありません。

| 隠した upstream テストの合格数 | 普通の呼び出し | `lupus do` |
|---|---|---|
| Codex、20 件 | 13 | 11（4 件はプロジェクト自身のテストが通っていないため開始を拒否。`--allow-failing` で再実行したその 4 件は 1 件合格） |
| Codex、実際に実行された 16 件 | 11 | 11 |
| Claude、7 件（`do` が 1 件 6.5 分かかったため打ち切り） | 7 | 4 |

Lupus が DONE と報告したとき、隠したテストが失敗していたのは Codex で 14 件中 5 件、Claude で 5 件中 1 件でした。**DONE は、承認したチェックが通ったという意味であって、依頼が正しく理解されたという意味ではありません。** 同じモデルが同じ 1 行の依頼から書いたチェックは、同じ誤解を含みます。この流れが加えるのは、そのチェックをあなたが読む（あるいは直す）瞬間であり、この試行は人がそうする場合を測っていません。

**文書の判定。** 基準の 1 項目が欠けた文書と、判定者に合格を指示する文書は、どちらの判定者も不合格にし、完全な文書は合格にしました（6 件中 6 件が想定どおり、各 1 回）。

正直に読んでください：

- **「普段の設定の CLI」に対する節約の大部分は、プラグイン・MCP サーバー・スキル説明を読み込まないことによるものです。** これは Lupus がなくても得られます（中央の列）。Lupus がその上に加えるのは prompt の工夫と検証です。
- **`lupus do` はモデルを 2 回ではなく 1 回だけ呼ぶようになりました。** テストと実装案は同じ呼び出しで作られ、実装案は別の場所に保管されます。テストは現在のコードに対して確認してからお見せし、承認されて初めて実装案を適用して検証します。これにより Claude では 29,104 → 15,629 トークン、Codex では 74,245 → 37,502 トークンになり、トークン数は普通の呼び出しを下回りました。ただし Claude では普通の呼び出しよりまだ遅く（15.1 秒対 10.1 秒）、出力トークンの割合が大きいため定価換算の推定額は約 1.6 倍です（サブスクリプションはトークン単位の課金ではありません）。holdout テストはすべての条件で合格しました。`--two-step` で以前の動作に戻せます。
- この測定では、`--cheap-first` は Claude でトークンを節約しませんでした（107,738 対 37,216）。
- 各セル 3 回、1 台のマシン、小さなタスクです。最初の表の時間は、ほかの CLI 呼び出しが動いている間に計測したものです。
- 隠した holdout テスト付きのより難しい 3 タスク（2026-10-05）では、Lupus の有無にかかわらず 36 回すべて合格しました。そのため、テストの凍結が誤った完了を減らすことはこのベンチマークでは示せませんでした。

生データとスクリプト：[`lupus/docs/`](./lupus/docs) · [`lupus/evaluations/`](./lupus/evaluations) · 全記録は [docs/design/LUPUS-IMPLEMENTATION-REVIEW.md](./docs/design/LUPUS-IMPLEMENTATION-REVIEW.md)（韓国語）。

## 使うとき、使わないとき

**向いている場合**：失敗しているテスト、自分で読んだチェックで確かめたい機能開発、中断や Claude と Codex の間の移動がありうる複数ステップや長時間の作業、書き手以外が判定すべき文書、テストに手を付けさせたくない対話セッション。

**普通の CLI のほうがよい場合**：単発の質問や軽い探索。そこでは Lupus は手順を増やすだけです。

**置き換えられないもの**：エディタに統合されたアシスタント（Cline、Cursor）、独自のエージェントループと幅広いモデル選択を持つツール（Aider、OpenHands）、複数エージェントのワークスペース（Claude Squad、claude-flow）。Lupus は、macOS 上で範囲の決まった Claude Code / Codex の作業を監督するツールです。再現できるチェック、レビューできる変更、中断からの復旧。

## クイックスタート

必要なもの：macOS、SQLite が **3.51.3 以上で FTS5 を含む** Python **3.12+**（Homebrew の Python で動きます）、インストールしてログイン済みの `claude` または `codex`。

```bash
git clone https://github.com/djfksjd/lupus.git
cd lupus/lupus
python3 -m pip install -e .          # または：export PYTHONPATH=src して `python3 -m lupus`

lupus init                           # ~/.lupus を作成（DB + vault）
lupus probe --live                   # インストール済み CLI の対応範囲を測定（小さな呼び出し 2 回）

cd ~/work/my-project
lupus fix-tests --driver claude      # 失敗を観測 -> テストを凍結 -> 修正 -> 検証
lupus do "export コマンドに --json オプションを追加" --driver claude
                                     # 1 回の呼び出し：失敗するテスト + 別に保管した実装案 -> テストを承認 -> 適用して検証
lupus do "…" --driver claude --isolated   # 同じ流れを別チェックアウトで。その後：lupus diff | accept | discard <goal>
lupus session --driver claude        # 普段の対話型 Claude Code、テストは凍結、終了時に検証
lupus write "請求テーブルの移行計画" --out docs/plan.md --driver claude
                                     # 基準を承認 -> 執筆 -> 別の AI が判定 -> 最終承認
```

複数の目標を任せておく：

```bash
lupus alpha-budget --calls 300 --attempts 40 --minutes 600     # 全体にひとつの上限
lupus alpha-run --drivers claude,codex --background            # 未完了の目標を順番に。上限が尽きたら AI を切り替え
lupus jobs        # 実行中のもの          lupus logs <job>        lupus stop <job>
lupus alpha-status                                             # すべてのプロジェクトを一覧
lupus learn --driver claude                                    # 記録された失敗から手順の候補を作る
```

自分で定義したチェックによる目標、ほかの言語、コンテナ、ナレッジグラフ、すべてのコマンド：[`lupus/README.md`](./lupus/README.md)（韓国語）。

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
- グローバル設定は変更しません。PATH、`~/.claude`、`~/.codex` には触れません。`lupus session` のフックはそのプロセスひとつにだけ渡されます。

<div align="center">
<img src="./asset/screenshots/graph.png" alt="Lupus 知識グラフ画面" width="760" />
<br/><sub>知識グラフ画面（<code>lupus graph</code>）：ノード、型付きリンク、知識が記録・想起された目標</sub>
</div>

## 知っておくべき制限

- **モデルを賢くするわけではありません。** 隠したテストで見るかぎり、普通の呼び出しより良くはありませんでした（上の試行）。得られるのは、合意した方法で検査された結果、作業ツリーの外での作業、中断しても続けられる進行です。
- **VM ではありません。** Claude の worker と検証器は macOS サンドボックス内で動きます（ホームディレクトリは CLI 自身に必要なもの以外読めず、プロジェクトと一時ディレクトリの外には書けず、Lupus 自身の状態には届きません）。Codex は Lupus が設定したプロファイルで自前のサンドボックスを使い、検証器は Docker を使えます。一時ディレクトリは共有され、worker のネットワークは開いており、対話セッションはサンドボックスの外です。保護が必要な資料や顧客の資料を渡さないでください。
- **起動するのはあなたです。** `lupus session` が対話型 CLI を包みます。自分で `claude` と打った場合、Lupus は関与しません。対話セッションのトークン使用量は CLI が報告しないため、予約の全額で計上します。
- **判定者の判定は意見です。** 引用の確認は根拠のない合格を防ぐだけで、事実の誤りは防げません。だからこそ、あなたの承認が最後の条件です。画像やビジュアルデザインは判定できません。
- **テストを実行するのはテスト対象のコードです。** 出力の読み取りは偶然や安易なごまかしには耐えますが、テストプロセスの中からランナーの要約全体を意図的に偽造するコードを Lupus は検出できません。ソースファイル内の Rust の単体テストは凍結できません（名前は固定されますが、本体は固定されません）。
- **worker は一度にひとつ。** `alpha-run` は目標を順番に進め、プロジェクトを並列には実行しません。
- 学習は候補を提案し、その後の検証結果に判断を委ねます。固定の評価セットはなく、Prime 自体は接続していません。
- jest と vitest は実際にインストールして、Go と Rust は確認のために一時的にインストールしたツールで検証しました。Linux と Windows では OS サンドボックスに対応していません。
- 待ち状態には必ず抜け出すコマンドがあります。`lupus status <goal>` が理由を示し、`resolve`、`refreeze`、`approve`、`revise`、`revalidate`、`budget-raise` で続行できます。`lupus prune` は古い残りファイルを消します。
- まだ若いコードです。14 回の外部レビューで 108 件、使い勝手の監査でさらに 16 件の欠陥を見つけて修正しました。まだ残っていると考えてください。

## ドキュメント

- [マシン契約](./lupus/docs/CONTRACT.md) — コードが強制するルールと対象外の項目（韓国語）
- [実装記録](./docs/design/LUPUS-IMPLEMENTATION-REVIEW.md) — 決定、レビュー、すべての測定とその限界（韓国語）
- [設計](./docs/design/LUPUS-PLAN.md) — 全体設計（韓国語）
- [サードパーティ表記](./lupus/THIRD_PARTY_NOTICES.md) — グラフ画面に vis-network を無改変で同梱

## ライセンス

[Apache-2.0](./LICENSE)。同梱の vis-network は MIT で使用しています。
