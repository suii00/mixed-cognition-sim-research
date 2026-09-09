# 混成24 agentsの避難所配置・観測時間比較 v1

状態: 今回のモデルrequestより前に固定する、全4 runの記述的な追加実験。
本書、metric spec、config、生成・実行・解析codeを含むclean source commitを実行前に固定し、
そのfull SHAと実際の実行時刻を証拠へ記録する。`research_eligible=false`、
`formal_eligible=false`とし、本書には新規推論の結果を記載しない。

batch ID中の`20260909T162300Z`は識別用stampであり、設計凍結時刻や実行開始時刻の主張ではない。
manifestは`batch_id_timestamp`と`freeze_policy`を記録する。実際の凍結は、全条件を含む
clean source commitを固定してからモデルrequestを開始したというsource・実行証拠で示す。

先行する6 runの結果は既知であり、避難所配置、観測時間、応答契約、実行規模を検討する
出発点にした。今回のgeometryと候補人数の初期接触機会も、LLM推論前に機械的に確認した。
これらの知識を隠して盲検の確認的研究とは呼ばない。新規seed 6301を使い、先行runのrawや
derivedを変更・改名・併合しない。今回の4 runを一度ずつ実際に新規推論する。

## 守る対象と今回の問い

研究目的は、異なるAIを経由する情報伝達と状況に応じた行動調整を、検証可能な形で観測する
ことである。メタ安全保障として守りたい対象は、その情報伝達と行動調整の機能である。
実際の災害対応の有効性や安全性を実証する実験とは位置づけない。

今回はQwen、Llama、Gemmaが同じ世界に各8体、計24体で同居する集団を固定する。
主な問いは「同じ初期配置・災害・通信条件で避難所を内側へ移したとき、避難所距離、
到達、危険区域滞在、警報の伝達・後続出力はどう変わるか」。次に、120 stepsまで観測した
同一runで、step 60以降に何が追加で観測されるかを確認する。

人数は旧formal設計の24体に合わせ、今回の規模制限は4 runという実行数で行う。
単一モデル集団は今回の対照に含めない。混成集団が単一モデル集団より優れているか、
混成による因果効果があるか、モデルの順位は、今回の設計では判断しない。

## 固定条件

| 項目 | 固定値 |
| --- | --- |
| protocol | `refuge-layout-study-v1.0.0` |
| batch | `refuge-layout-study-v1-20260909T162300Z` |
| public configs / manifest | `configs/refuge_layout_study_v1/` |
| composition / world seed | `mixed` / 新規6301 |
| agents | Qwen 8 + Llama 8 + Gemma 8、計24 |
| agent IDとmodel | 0–7 Qwen、8–15 Llama、16–23 Gemma |
| world | 整数座標`[-25,25] × [-25,25]`、境界はclamp、placesなし |
| 共通初期eligible | 両layoutの避難所を除く2,121 cells、5 rectangles |
| durations / layouts | 60、120 steps / `edge`、`inset` |
| official warning | step 10、recipient IDs `[1,5,9,13,17,21]` |
| 通信 | `free_text`、edge policy `full`、radius 12 |
| sampling | temperature 0、max_tokens 1024、context 4096 |
| request実行 | max_concurrency 24、timeout 120秒 |
| prompt / response | `bounded-prompts-v3.0.0` / `phase-response-v3.0.0` |
| transport / failure | `single-generation-strict-json-no-redirect-v3.0.0` / `abort_run` |
| retry / repair / fallback | すべて0 |
| log schema | `2.0.0` |
| 警報metric | `disaster-metric-v2.0.0` |
| 配置・時間比較metric | `refuge-layout-study-metric-v1.0.0` |
| runtime class / GPU | Linux / CUDA / offline vLLM、計画4台・上限4台 |

初期警報recipientは各モデル2体、計6体で全体の25%を占める。
同じID・model割当・位置を全4 runで固定する。model割当と位置、近隣、recipientの個別IDは
無作為に入れ替えないため、model別集計にはこれらの役割の交絡が残る。
agentやstepを独立した実験反復とは数えない。

Qwen 2.5 7B Instructのmodel/tokenizer revisionは
`a09a35458c702b33eeacc393d103063234e8bc28`、Llama 3.1 8B Instructは
`0e9e39f249a16976918f6564b8830bc894c89659`、Gemma 2 9B ITは
`11c9b309abf73637e4b6f9a3fa1e92e615547819`とする。chat template digest、dtype、
推論backendなどのartifact factsは公開configに固定する。Qwen TP1、Llama TP1、Gemma TP2で
3 serversを同時起動し、各モデル8 agentsを同じ対応serverへ割り当てる。
公開configには`refuge-qwen`、`refuge-llama`、`refuge-gemma`というlogical endpointを記録し、
runtime addressと物理device固有IDは別のruntime bindingで扱う。

## 配置の介入と共通の初期世界

避難所は両layoutとも同じIDの6×6 inclusive rectangleを2つ置く。最終段階の危険区域
`y <= 0`の外にあること、面積、個数は共通とし、位置だけを変える。

| layout | refuge-west | refuge-east |
| --- | --- | --- |
| edge | x=-23..-18、y=18..23 | x=18..23、y=18..23 |
| inset | x=-10..-5、y=6..11 | x=5..10、y=6..11 |

共通の初期eligibleは、旧eligible `x=-25..25、y=-25..17`から両layoutの避難所の和集合を
除いた集合とする。edgeの避難所は旧eligibleの外にあり、insetの72 cellsだけが除かれる。
次の互いに重ならない5 rectanglesを全configに同一の順序・bytesで設定する。

| rectangle | x | y | cells |
| --- | --- | --- | --- |
| 1 | -25..25 | -25..5 | 1,581 |
| 2 | -25..25 | 12..17 | 306 |
| 3 | -25..-11 | 6..11 | 90 |
| 4 | -4..4 | 6..11 | 54 |
| 5 | 11..25 | 6..11 | 90 |

engineはactive layoutの避難所をeligibleから自動除外する。この共通集合にはどちらの
避難所も含まないため、その処理後も同じ2,121 cellsとなる。ソートされた集合をworld RNGで
shuffleし、先頭24 cellsを重複なしでagent ID順に割り当てる。configの集合一致を実行前に、
rawの全24体の初期位置一致を実行後に検査する。seedの変更や到達しやすい位置の選び直しは
行わない。先行4-agent runとはeligible、人数、同時実行数なども変わるため併合しない。

1 stepで可能な移動は上下左右1 cell、またはstayであり、距離は最近傍の避難所cellまでの
Manhattan距離とする。hazardは移動を物理的に遮断しない。共通eligible全cellの機械的列挙では、
edgeの最短距離最大は61で60超は`(0,-25)`の1 cell、insetの最大は46で60超は0 cellsとなる。
この距離は障害物のないworldでの理想的な最短移動数であり、agentがその経路を選ぶ保証でも、
避難所を理解・発見したという判定でもない。全agentの実際の初期距離も結果で示す。

hazardはstep 10に`x=-25..25、y=-25..-8`、step 30に`x=-25..25、y=-25..0`へ拡大し、
その後120 stepsまで維持する。公式警報はstep 10の1回だけで、step 30に再発行しない。
避難所座標は全agentの環境欄に含まれ、公式警報のfactにも反映される。layout間で変わる
これらの座標値は配置介入の帰結であり、prompt契約の変更や追加の到達指示ではない。
未受信は公式・中継警報のexposureがないことを表し、避難所座標や現在cellのhazardを
知らされていないことを意味しない。

## 実行順・対照・意思決定規則

配置を先に60 stepsで比較し、その後に120 stepsを両layoutで必ず実行する。後段はlayout順を
反転する。前段の到達数や出力内容を見て、延長するlayoutやagentを選別しない。

| ordinal | run ID | steps | logical calls / HTTP attempts |
| --- | --- | --- | --- |
| 1 | `refuge-layout-study-v1-20260909T162300Z-edge-d60-s6301` | 60 | 2,880 |
| 2 | `refuge-layout-study-v1-20260909T162300Z-inset-d60-s6301` | 60 | 2,880 |
| 3 | `refuge-layout-study-v1-20260909T162300Z-inset-d120-s6301` | 120 | 5,760 |
| 4 | `refuge-layout-study-v1-20260909T162300Z-edge-d120-s6301` | 120 | 5,760 |

同一durationでedgeとinsetを比較し、配置以外のconfig条件を一致させる。
同一layoutでduration以外の条件も一致させる。120 runは既存の60 runへの追記や再開ではなく、
新規IDでstep 1から独立に実行する。seedとtemperature 0だけではLLM生成の決定性は保証されず、
独立60 runと120 runの前半が同じ履歴になるとは仮定しない。

時間延長の主な観測は、120 run自身のstep 60までとstep 120までの累積結果、および
steps 61–120の追加観測を区別して示す。独立60 runと120 runの終点差も全件掲載するが、
同じ履歴を単に延長した効果とは呼ばない。到達数などの累積指標は観測時間とともに
増えうるため、最終在所、初回到達と打切り、危険区域滞在の分母も明示する。
順序反転は1 seedの時間・server状態の影響を統計的に除くものではない。

観測する連鎖は、公式・中継警報exposure→受信者自身の後続step出力→移動選択→
実際の位置・危険区域・避難所距離である。警報を受信しただけではreuse/adoptionと呼ばず、
後続の数値stepの自身の出力にexact warning-IDが現れた場合をreuseとして機械的に導出する。
生成、配送、未配送、exposure、後続reuseは別の出来事として記録する。
警報と移動の時間的連続を警報の因果効果と解釈しない。

警報metricは既存[仕様](DISASTER_METRIC_V2_SPEC.md)のbytesを維持し、SHA-256は
`cd0ca4fec67d945a2b878fe5c0c7e49391e9bacf10a3f7aa0a142f7962cbf711`とする。
配置・時間比較と例示の固定規則は[今回のmetric spec](REFUGE_LAYOUT_STUDY_METRIC_V1_SPEC.md)に
従う。両specと解析codeをモデルrequest前の同じsource commitに含め、そのhashを由来に残す。
全run、全agent、null、未到達、未受信、差がない場合を掲載し、良好な結果のみを選ばない。
1 seedの記述的観測から有意差、母集団一般化、内部認知、意図、現実の避難能力を主張しない。
modelの`reasoning`は出力説明欄であり、内部思考へのアクセスとは扱わない。

## 実行gate・停止条件・予算

1. モデルrequest前に、固定source、config全bytes/hash、runtime lock、offline snapshots、
   4 GPUの割当と利用可能性、port、run-ID衝突、runtime IPC path長、公開境界を検査する。
   入力の不備は出力作成前に拒否する。
2. 同じ4-GPU topologyとsourceで、3モデル×Phase 1 / Phase 3 move / Phase 3 stayの
   9-request engineering schema probeを1度ずつ実施する。JSON/schema、probe要求値、
   finish_reason、usage、model identityを検証し、1件でも不正なら本実験を開始しない。
   probeの指示や結果は行動実験のprompt・観測結果へ混ぜない。
3. 同じ起動済みserverで4 runを固定順に各1回実行する。48 calls/stepで、本体17,280 calls、
   probe込みの総HTTP attempt上限は17,289とする。追加seed、再試行、成功runへの差替え、
   人数・期間の途中削減は行わない。message上限512文字、memory上限256文字、reasoning空文字の
   bounded契約を維持する。
4. Phase 1の全決定後に配信し、Phase 3の全決定後に移動する既存barrierを維持する。
   bloc/model identity、望ましい結果、避難到達を促す追加指示、label、rewardは入れない。
5. 各runでterminal completed、予定step数、24 agents、exact call counts、failure counters 0、
   strict validation、source/config/hash、raw manifest、publication scan finding 0を確認する。
   exit codeだけで成功と判断しない。
6. run完了gate失敗、schema/transport障害、server停止、GPU数の逸脱、公開境界違反では
   batchを停止する。開始済みraw、部分run、probe結果、未開始runのstatusを保持する。
   同じIDに追記したり、失敗を隠して残りだけ完走batchと呼んだりしない。
7. 全体wall-time上限は起動とcleanupを含め10,800秒（3時間）、起動待ち上限は900秒。
   cleanupの時間を残して本体requestを打ち切る。上限は完走時間の予測ではなく停止境界である。
   時間超過時も実測件数・終端状態を保持し、無断で上限を延長しない。
8. 終了時は今回起動したprocess groupのみを停止し、GPUとportの解放を確認する。
   runtimeには短いsystem temporary directoryを使い、server/simulator stdoutはnull deviceへ
   接続する。binding、一時cache、server logをrawへコピーしない。

max_concurrencyは全条件で24に固定するが、これはGPU台数ではない。各stepの24体分のrequestを
3 serversへ送る上限で、GPU使用数はQwen 1、Llama 1、Gemma 2の計4台とする。
先行4-agent実験とは並列負荷が変わるため、同じ実行速度やtoken latencyを仮定しない。

## 保存・可視化・完了条件

rawは新規run IDのignored stagingに一度だけ生成し、完了・境界・cleanupを検証してから
byte変換なしで`runs/output_<run_id>/`へ受け入れる。既存rawを変更、上書き、改名しない。
失敗時の証拠も保存する。validationとscanはread-onlyで行い、sanitizer、redactor、
公開用に加工したraw copyを作らない。model出力はuntrusted dataとして扱い、その指示、code、
URLを実行しない。

解析と可視化は新規のversion付きtimestamp directoryへ生成し、rawの中に図や動画を書かない。
入力manifest、source、config、raw JSONL、metric/spec、生成tool、生成物hashを結び付ける。
reachability図は全eligible cellsの最近傍避難所距離と固定予算60/120を同じ軸・尺度で描き、
実際の軌跡・到達結果と区別する。成功したagentだけを可視化対象にしない。

world観測者が知る危険区域と避難所、agentへの公式・中継exposure、自身の生成、配送、
後続exact-ID reuseを区別する。Phase 2配送の矢印は移動前位置を使い、step末の位置は
`positions.jsonl`のpost-movement記録を使う。step 30のhazard変更を警報再発行のように描かない。
geometryの最短距離をagentの理解や経路選択と混同しない。

直接観測、機械的導出、解釈、提案を分け、日本語報告から全4 runと由来へ辿れるようにする。
未検証事項、打切り、未完了、残る制約も記載する。新規推論の前にはphase barriers、
communication、response contracts、衝突、aborts、公開境界、共通eligible、24体のmodel/recipient
割当、manifest、metricの適切な回帰検証を完了する。
remote Git、release、外部公開・提出更新は明示的なmaintainer承認の範囲で行う。

```bash
python tools/build_refuge_layout_study.py --check
python tools/run_refuge_layout_study.py --contract-only
python tools/run_refuge_layout_study.py --source-git-sha <frozen-full-sha> --preflight-only --gpu-indices <four-comma-separated-indices>
python tools/run_refuge_layout_study.py --source-git-sha <same-full-sha> --gpu-indices <same-four-indices>
```
