# GitHub Pagesでの成果物公開

[公開成果物一覧](https://suii00.github.io/mixed-cognition-sim-research/)から、保存済みHTMLを直接ブラウザーで開ける。HTMLのダウンロードは必須ではない。警報保持の比較HTMLは約56 MiBあるため、初回の読み込みに時間がかかる場合がある。GitHub上の保存済みファイルをダウンロードしてローカルで開く方法も利用できる。

## 公開対象と来歴

`site/index.html`を入口とし、`site/assets.json`に明記した11ファイルを同じ相対パスで配信する。

| 内容 | ファイル | 保存元commit |
|---|---|---|
| 警報保持方式の比較 | HTML 1本 | `b10477a86e1096f5fb9b1086545ff4e1d86effb6` |
| 避難所配置・60/120 step比較 | HTML 4本、PNG 4枚 | `b10477a86e1096f5fb9b1086545ff4e1d86effb6` |
| 日本語promptの過去実行 | HTML 1本、MP4 1本 | `4b1632848ba9f09fddddf951a6e18b609d4603b0` |

ここで保存元commitは配信ファイルのGit blobをたどる参照であり、推論sourceや解析sourceとは区別する。それらの実験来歴は各結果レポート・元のmanifestに記録されている。

過去の日本語prompt実行は`artifacts/engineering-ja-20260830`から配信されていた。そのHTMLとMP4だけを上記commitからバイト単位で同一のままmainへ収録し、元の`derived/output_engineering-ja-qwen25-swallow05-elyza8b-24a60s-s2403-20260830-r001/`以下のURLを維持した。旧branchのengineや設定をmainへ混ぜてはいない。旧branchとcommitは残し、当時の仕様・rawは固定commitへのGitHubリンクで参照する。

既存のraw JSONL、実験用config、解析結果、HTML本体、manifest、hashを変更せず、Pages用の再解析・再生成やGPU実験も行わない。生ログ・config・source・manifestの参照先はGitHubに置く。

## 公開処理

[`.github/workflows/pages.yml`](../.github/workflows/pages.yml)は、main宛てPRで検証とPages artifactの作成までを行う。mainへのpush、またはmainを指定した手動実行では、そのartifactをGitHub Pagesへdeployする。PRや他branchからのdeployは行わない。deploy jobは`github-pages` environmentを使い、PagesとOIDCの書き込み権限をそのjobだけに付与する。

repositoryのSettings → Pages → Build and deploymentのSourceは**GitHub Actions**とする。`github-pages` environmentのdeployment branch policyは**main**に限定する。専用の集約branchやbranch名変更は不要。新しい成果物はmain上の明示的な配信一覧へ追加する。

[`tools/build_pages_site.py`](../tools/build_pages_site.py)は、一覧のpath、ファイルサイズ、SHA-256、Git blob ID、公開境界、ローカルリンク先を先に検証する。symlink、hard link、Windows reparse point、既存出力への上書き、`runs/`や`derived/`への出力を拒否する。入力が不正な場合は出力ディレクトリを作らない。新しい`.tmp/pages-site_<unique-id>`にindexと一覧、明記した成果物だけをバイト単位で同一のまま格納する。HTMLを加工するsanitizerや公開用の変換処理は設けない。

```bash
# 読み取り検証のみ。出力は作らない。
python tools/build_pages_site.py --check
python -m pytest -q tests/test_pages_site.py

# 未使用のUTC timestampを用いたディレクトリへ配信ファイルを格納。
python tools/build_pages_site.py
```

追加時は、公開済みまたは公開境界を満たす保存済み成果物のpath・bytes・SHA-256・Git blob・保存元commitを`site/assets.json`へ記録し、indexからリンクする。コピー対象の自動探索やrepository全体の配信は行わない。PRで既存CIとPages buildを確認してからマージする。実験の来歴として途中commitのSHAを参照するため、通常の**Create a merge commit**を使う。

配信内容を戻す場合はPages workflowを残したまま、mainのindexと配信一覧を以前の構成へ戻すPRを作り、Actionsから再deployする。今回の移行前の旧サイト全体へ戻す場合はSourceをDeploy from a branch、branchを`artifacts/engineering-ja-20260830`、pathを`/`へ戻し、environmentのbranch policyも対応させる。生ログや旧branchは削除しない。

## 過去runの再解析

過去runの再解析は各結果レポートに記載した**解析source commit**の作業ツリーで実行する。最新mainでは解析関連ファイルが更新され、旧実験の凍結sourceとの一致検査に通らない場合がある。一致検査を外したり、旧runを書き換えたりしない。

避難所配置実験の場合、既存のcheckoutを維持して別の作業ツリーを作る例は次のとおり。作業先は未使用のパスを指定する。

```bash
git worktree add --detach ../mixed-cognition-refuge-reanalysis d6fa9ad6c2b4ecb539eb488de356c49bbb75ef88
cd ../mixed-cognition-refuge-reanalysis
```

そのcommitの依存関係を用意し、[避難所配置の結果レポート](RESULTS_REFUGE_LAYOUT_STUDY_20260910.md)末尾の再解析コマンドを実行する。保存済みrawからの再解析にはGPUは不要である。

## 確認範囲

配信前に全11成果物のhash、byte数、indexの相対リンク、公開対象の限定、入力拒否・出力衝突の境界を検証する。Actionsのbuild/deployの成否と配信commitはGitHub上で確認できる。HTMLの実ブラウザー表示・操作の最終確認は利用者が行う。実ブラウザーで確認できていない状態を、表示検証済みとは報告しない。
