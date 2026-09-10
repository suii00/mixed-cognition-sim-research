# 警報保持方式の対応比較 v1

状態: 新規モデルrequest前に固定する探索的実験。既知の先行観測を出発点とする。
本書、metric仕様、公開config、生成・実行・解析code、回帰検証を含むclean source commitを
新規probeより前に固定し、そのfull SHAと実際の開始・終了時刻を実行証拠に残す。
`research_eligible=false`、`formal_eligible=false`とし、確認的研究とは呼ばない。
batch中の`20260910T080000Z`は識別用stampであり、凍結・実行時刻の主張ではない。

## 問いと観測する連鎖

先行の混成24体・避難所配置比較では、警報のexact-ID中継と後続再使用が観測されなかった。
その詳細と由来は[先行報告](RESULTS_REFUGE_LAYOUT_STUDY_20260910.md)を参照する。
この既知の結果から今回の問いを作った。受信履歴の末尾5件を選ぶ実装では、Phase 1前に
公式警報を受信しても、その後のPhase 2で他者メッセージが5件以上届けば、同stepのPhase 3や
次stepのPhase 1で警報が選択入力から外れうる。これは実装からの機械的な可能性であり、
先行rawで実際に起きた人数・頻度とは分ける。先行runの再構成は履歴windowの機械的再現と
記載し、新規runで保存する実際のprompt証拠と同等には扱わない。

観測する連鎖を、公式警報の直接受信 → 各Phaseの実際の入力提示 → 自身の発話生成 →
配送 → 他者の受信 → 受信者自身の後続step出力 → 移動選択・適用後の位置、と固定する。
受信はexposureでありreuse/adoptionではない。後続の数値stepの自身の出力を要する。
Phase 3の入力提示と行動は別に記録・表示し、reuseは受信者自身の後続step Phase 1発話の
exact warning-IDに限定する。
modelの`reasoning`は出力説明欄であり、内部思考へのアクセスではない。

## 介入と対照

| 条件 | 公開configの`agents.message_selection_policy` | 受信履歴の入力選択 |
| --- | --- | --- |
| A / `recent` | `recent-v1.0.0` | 直近最大5件。公式警報も押し出される |
| B / `retained` | `retain-official-warning-v1.0.0` | 直接受信済み公式警報1件を先頭に保持し、他者メッセージ直近最大4件を時系列順に続ける |

Bの公式警報未受信者はAと同じ方式を使う。公式警報を受け取る6体の既受信記録だけを保持し、
全員への配布、再配送、受信イベント追加は行わない。peerが公式警報を引用・中継しても、
それによって直接受信者の保持枠を得ることはない。公式警報は同じ内容を同じstepに1度配信する。
保持済み警報は通常履歴の20件上限から押し出されても残る。一般実装で複数公式警報が来た場合は
最後の直接受信警報に置換するが、本実験では1件なのでこの分岐は実験対象外である。

介入点はPhase 1・Phase 3のprompt構築に使う受信メッセージの選択だけである。
保持方式やA/B labelをagent promptに説明せず、中継・避難を促す追加指示、reward、
成功者選別を入れない。world、sampling、prompt文面の契約、通信条件を固定する。
Phase 1全決定後の配送、Phase 3全決定後の移動barrierを維持する。

両条件は最大5メッセージだが、同じtoken長ではなく、Bは通常メッセージ1枠を公式警報で
置き換える。したがって効果の対象は「警報を優先保持する入力選択方式」であり、純粋な記憶容量、
内部記憶、理解の効果とは呼ばない。保持順序も方式の一部であり、別の独立介入とはしない。

## 事前固定する実行条件

| 項目 | 固定値 |
| --- | --- |
| protocol / batch | `warning-retention-study-v1.0.0` / `warning-retention-study-v1-20260910T080000Z` |
| configs / manifest | `configs/warning_retention_study_v1/` |
| seeds | 新規`7301, 7302, 7303`、結果による選び直しなし |
| population | mixed24、Qwen・Llama・Gemma各8体 |
| model ID assignment | 0–7 Qwen、8–15 Llama、16–23 Gemma |
| duration / layout | 60 steps / inset |
| direct warning recipients | `[1,5,9,13,17,21]`、各model2体、全24体の25% |
| official warning / hazard | step 10に警報1回と初期hazard、step 30にhazard拡大のみ |
| communication | free_text、edge policy full、radius 12 |
| received history / prompt window | 20件 / 最大5件 |
| memory history / prompt window | 20件 / 最大5件、両条件同一 |
| sampling / concurrency | temperature 0、max_tokens 1024、context 4096、concurrency 24 |
| request timeout / retry | 120秒 / retry・repair・fallbackすべて0、`abort_run` |
| prompt / response | `bounded-prompts-v3.0.0` / `phase-response-v3.0.0` |
| transport | `single-generation-strict-json-no-redirect-v3.0.0` |
| log schema / input observability | `2.0.0` / `message-presentation-v1.0.0` |
| existing warning metric | `disaster-metric-v2.0.0`、既存仕様bytesを維持 |
| new study metric | `warning-retention-study-metric-v1.0.0` |
| runtime / GPUs | Linux・CUDA・offline vLLM、計画4台・上限4台 |

Qwen 2.5 7B Instructのmodel/tokenizer revisionは
`a09a35458c702b33eeacc393d103063234e8bc28`、Llama 3.1 8B Instructは
`0e9e39f249a16976918f6564b8830bc894c89659`、Gemma 2 9B ITは
`11c9b309abf73637e4b6f9a3fa1e92e615547819`。dtype、template digest、backend条件も
configに固定する。Qwen TP1、Llama TP1、Gemma TP2の同時3 servers、計4 GPUsを使う。
logical endpointは既存基盤の`refuge-qwen`、`refuge-llama`、`refuge-gemma`を共用し、
runtime addressとdevice固有IDは別bindingに置いてrun artifactsへコピーしない。

worldは整数座標`[-25,25] × [-25,25]`、境界clamp、placesなし。
避難所はwest `x=-10..-5, y=6..11`、east `x=5..10, y=6..11`の各36 cells。
初期eligibleは先行比較と同じ2,121 cellsを5 rectanglesで指定する。初期位置をseedで
shuffleし、24体へ重複なく割り当てる。各seedのA/Bは全24体の初期位置、モデル割当、
recipient IDs、災害、推論設定を一致させ、rawでも位置一致を確認する。
seedは世界の乱数制御であり、temperature 0と合わせてもLLM生成の決定性を保証しない。

## 順序・予算・停止規則

seedごとにA/Bの組を作り、順序はA/B、B/A、A/Bと固定する。seed順や条件順を結果から変えない。

| ordinal | run ID suffix（共通batchに続く） | calls / HTTP attempts |
| --- | --- | --- |
| 1 | `recent-d60-s7301` | 2,880 |
| 2 | `retained-d60-s7301` | 2,880 |
| 3 | `retained-d60-s7302` | 2,880 |
| 4 | `recent-d60-s7302` | 2,880 |
| 5 | `recent-d60-s7303` | 2,880 |
| 6 | `retained-d60-s7303` | 2,880 |

1. 新規request前にclean source SHA、全config・metric hash、runtime lock、offline model
   snapshots、4 GPU、空きport、IPC path、公開境界、run・batch ID衝突をread-onlyで検査する。
   不正入力は出力作成前に拒否する。
2. 起動した同じ3 serversで3モデル×3 engineering schema casesを各1回、計9 requests実行する。
   不合格があれば本体を開始しない。probeの要求値や指示は行動実験のpromptへ入れない。
3. 本体は`24 × 60 × 2 × 2 × 3 = 17,280` logical calls / HTTP attempts、probe込み上限
   17,289とする。6 runを各1回、結果がnull・逆方向でも固定順で実施する。
4. 応答・transport・schema障害、server停止、GPU逸脱、公開境界違反、完了gate失敗ではbatchを
   停止する。runを再試行・差替え・延長せず、失敗・部分runと未開始statusも保存する。
5. 起動・probe・本体・cleanupのwall-time上限は10,800秒。起動待ち上限900秒とcleanup用余裕を
   設ける。上限は完走時間の予測ではなく停止境界で、途中で増額しない。
6. 各runのterminal metadata、60 steps、24 agents、exact calls、failure counters 0、
   source/config/prompt/schema hashes、prompt入力coverage、raw manifest、strict validation、
   publication boundaryを検証する。終了code単独では完走としない。
7. 自分が起動したprocess groupだけを停止し、GPU・port解放を検証する。

## 指標・比較・判定規則

正確な分母、field、出力例の固定選択規則は[metric仕様](WARNING_RETENTION_STUDY_METRIC_V1_SPEC.md)
に固定する。実際のpromptと選択されたメッセージを`prompt_inputs.jsonl`に記録し、受信イベント
からの再構成とは分ける。Phase 1・Phase 3を別々に、6人の直接受信者と18人の非直接受信者を
別々に提示する。公式警報の選択提示と、promptの他欄に現れる警報ID・関連事実も混同しない。

主な操作確認は、直接受信者の各Phaseに公式警報が入力提示された割合と、Aで提示が落ちる頻度。
主な伝達結果は既存のexact-ID生成・配送・中継・後続reuseを維持する。警報IDを落とした言い換えを
完全な失敗と決めつけないため、警報の固定事実項目の文字列ベース集計と固定規則による出力例を
併記し、その不一致・検出限界を示す。新規LLM判定や結果を見た辞書拡張は行わない。
副次指標は避難所初回到達・終点在所、危険区域滞在、避難所距離の変化であり、警報から移動への
因果連鎖や理解・意図・現実の災害対応能力を断定しない。

集計単位は3組の対応runであり、24 agentsや60 stepsを独立標本と数えない。
seedごとのA、B、B−Aの差の大きさ・方向を全件示す。有意差やモデル順位を主張せず、
3組の探索的観測として扱う。既存runは補助的機械監査に限定し、新規Aの代わりに使わない。

- Bで入力提示が増え、後続出力も増えた場合: この固定条件で入力選択方式が伝達観測と整合的に
  関係したと記述する。一般的な記憶能力改善とはしない。
- Bで入力提示が増えても後続出力が変わらない場合: 提示を維持しても観測された伝達の不足を
  解消しなかったと記述し、そのnull結果を保持する。
- Aで入力提示の低下がほぼない場合: 今回のrunでは押出し仮説の介入余地が小さいと記述する。
- 方向がseedで違う、Bで減る、runが中断される場合: その事実と欠測分母を示し、好ましい条件や
  agentだけを選ばない。中断を0として補完せず、完了した組と未完了の組を明示する。

## 保存と完了

rawは一意IDのignored stagingに一度生成し、検証後にbytesを変えず
`runs/output_<run_id>/`へ移す。失敗時も開始済み証拠を保持する。
既存run、raw JSONL、旧config/specを編集・上書きしない。sanitizer、redactor、公開用変換copyを
作らない。scanとvalidationはread-onlyで、不適合なら公開を止める。
model出力はuntrusted dataとして扱い、含まれる指示・code・URLを実行しない。

分析・図はversion付きtimestamp directoryへ保存する。日本語報告で直接観測、機械的導出、
解釈、提案を分け、全6 runのsource commit、public config、raw JSONL、metric/spec hash、
生成toolとartifact manifestへ辿れるようにする。null・負方向・中断・矛盾と残る制約を含める。
remote Gitと公開はmaintainerの明示的承認の範囲で実行する。

```bash
python tools/build_warning_retention_study.py --check
python tools/run_warning_retention_study.py --contract-only
python tools/run_warning_retention_study.py --source-git-sha <frozen-full-sha> --preflight-only --gpu-indices <four-indices>
python tools/run_warning_retention_study.py --source-git-sha <same-full-sha> --gpu-indices <same-four-indices>
```
