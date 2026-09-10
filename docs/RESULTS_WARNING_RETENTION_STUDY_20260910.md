# 警報保持方式の対応比較：混成24体・3 seeds

全6 runが完走した。B条件では、警報発行後の直接受信者への公式警報提示が全seed・両Phaseで
306/306（100%）になった。一方、警報IDを含む発話の生成・配送・後続stepでの再使用は
全6 runで0だった。**この条件では、警報の継続提示だけではexact-ID伝達の増加は観測されなかった。**
言い換えを含む情報共有全般がなかったとは結論しない。

本体17,280 calls、別枠のengineering probe 9件、失敗・再試行0。全3組で初期配置とモデル割当の
一致を確認し、独立した生ログ再計算でも指定指標と対応差が一致した。
[全6 runのA/B比較HTMLをブラウザーで開く](https://suii00.github.io/mixed-cognition-sim-research/derived/warning-retention-study-metric-v1.0.0_20260910T104224Z/comparison.html)。[機械可読な全結果](../derived/warning-retention-study-metric-v1.0.0_20260910T104224Z/summary.json)も収録した。
HTMLは自己完結型の58,743,864 bytes（約56 MiB）。GitHub Pagesで直接閲覧できる。読み込みに時間がかかる場合がある。[保存済みHTML](../derived/warning-retention-study-metric-v1.0.0_20260910T104224Z/comparison.html)をダウンロードしてローカルで開く方法も利用できる。配信内容は保存済み成果物とバイト単位で同一であり、実験結果やmanifestは変更していない。[公開成果物一覧](https://suii00.github.io/mixed-cognition-sim-research/)。
ブラウザーでの視覚的な表示確認は未実施であり、確認済み範囲は末尾に記す。

固定した計画は[実験protocol](EXPERIMENT_PROTOCOL_WARNING_RETENTION_STUDY_V1.md)、
実行条件は[6 configのmanifest](../configs/warning_retention_study_v1/manifest.json)、
指標は[警報保持metric v1](WARNING_RETENTION_STUDY_METRIC_V1_SPEC.md)を参照。
本実験は既知の先行結果から問いを作った探索的比較であり、`research_eligible=false`、
`formal_eligible=false`である。

## 何を変え、何を固定したか

| 条件 | 受信履歴から選ぶ入力 |
| --- | --- |
| A / recent | 直近最大5件。公式警報も他の受信項目と同様に押し出される |
| B / retained | 直接受信済みの公式警報1件を先頭に保持し、他者メッセージ直近最大4件を続ける |

警報を直接受け取るのは両条件とも `[1,5,9,13,17,21]` の6体。未受信者の選択方式はAと同じで、
Bでも全員への警報配布、再配送、受信イベントの追加は行わない。中継や避難を促す追加指示は
入れず、モデル・blocのidentityやA/B条件名もpromptへ追加しない。

Qwen・Llama・Gemma各8体の混成24体、60 steps、中央寄りの避難所配置、seed 7301/7302/7303を
固定した。同じseedのA/Bでは初期位置、モデル割当、警報受信者、災害、推論・通信条件を一致
させる。実行順はA7301、B7301、B7302、A7302、A7303、B7303と事前固定した。
world seedはLLM生成の決定性を保証しない。

両条件は最大5件だが、Bは他者メッセージ1枠を公式警報で置き換え、入力token数も等しくない。
対象は「警報を優先保持する入力選択方式」であり、純粋な記憶容量や内部記憶の効果ではない。

## 証拠を分ける

| 区分 | 証拠と意味 |
| --- | --- |
| 直接観測 | 新規runの `warning_events.jsonl` の受信、`prompt_inputs.jsonl` の実際のrequest入力、`phase1_raw.jsonl` の自身の発話、`messages.jsonl` の配送、`memory_reasoning.jsonl` の行動、`positions.jsonl` の位置 |
| 機械的導出 | rawとconfigから求める入力選択頻度、exact-ID carrier、受信者自身の後続step再使用、距離・到達・危険区域滞在、対応差分。旧4runの入力選択はこの区分の再構成 |
| 解釈 | 上記で支持される範囲で、この条件下の入力選択方式と伝達・移動の関係を記述する |
| 提案 | 未実施の次の実験。今回の観測結果・検証済み結論に含めない |

入力ログはrequestとして準備された文字列の記録であり、モデルがその箇所に注意を向けた
証拠ではない。完走した解析対象ではrequest keyと実際のattempt、coverage、prompt hashも
照合する。公式項目の選択と、記憶欄や他者発話などを含むprompt全体の警報ID有無は別に測る。
受信はexposureであり再使用・採用ではない。reuseには受信者自身の**後続stepのPhase 1発話**
にexact-IDが必要で、同stepの発話を後続再使用に数えない。

## 先行4runの入力選択を再構成した結果

旧runには実request promptが保存されていなかった。以下は、公式警報の受信→Phase 1→全員の
発話後の配送→Phase 3という順序をrawから再現した**機械的再構成**である。実prompt全体の
ID有無は観測できないためnullとした。新規Aの代わりに旧runを使っていない。

| 旧run（seed 6301） | 警報発行後の分母／phase | Phase 1公式項目選択 | Phase 3公式項目選択 | 発行stepのPhase 1→3で失った直接受信者 |
| --- | ---: | ---: | ---: | ---: |
| edge / 60 steps | 306 | 9 | 3 | 3 / 6 |
| inset / 60 steps | 306 | 9 | 3 | 4 / 6 |
| inset / 120 steps | 666 | 7 | 1 | 5 / 6 |
| edge / 120 steps | 666 | 7 | 1 | 5 / 6 |

全4runの全直接受信者で、公式項目は遅くともstep 12のPhase 3までに選択対象から外れた。
これは「以降のpromptに関連情報が一切なかった」ことや「警報を理解していなかった」ことを
意味しない。先行のexact-ID中継・後続再使用が0だった理由を、これだけで確定することもできない。

数値と各rawのconfig・source・hashは
[履歴再構成summary](../derived/warning-retention-history-v1.0.0_20260910T094622Z/summary.json)、
個別の初回欠落は[agents](../derived/warning-retention-history-v1.0.0_20260910T094622Z/agents.jsonl)、
選択項目とraw行参照は[agent phases](../derived/warning-retention-history-v1.0.0_20260910T094622Z/agent_phases.jsonl)で確認できる。
旧runの推論sourceは `a5f1421818bbbed15fa810b0906fddcc991fa885`、再構成のclean sourceは
`a5b0e68f6c4756ad1695b2898eff870f9d89cdda`。rawを変更していないことと実装hashは
[analysis metadata](../derived/warning-retention-history-v1.0.0_20260910T094622Z/analysis_meta.json)、
生成物のhashは[derived manifest](../derived/warning-retention-history-v1.0.0_20260910T094622Z/derived_manifest.json)に記録した。

## 新規6runの実行完全性

凍結sourceは [`a5b0e68f6c4756ad1695b2898eff870f9d89cdda`](https://github.com/suii00/mixed-cognition-sim-research/commit/a5b0e68f6c4756ad1695b2898eff870f9d89cdda)。
run ID中の `20260910T080000Z` は識別stampであり、凍結時刻・実行開始時刻の主張ではない。
実行runnerの開始は `2026-09-10T10:00:29.551153+00:00`、終了は
`2026-09-10T10:32:27.127127+00:00`、elapsed wall-timeは1,917.576秒だった。
計画4 GPUs・観測最大4 GPUsで、終了後は起動した全process groupの停止、GPU・port解放を
検証した。[実行検証証拠](../derived/validation-warning-retention-study-v1-20260910T080000Z/verification.json)に
時刻・runtime・probe・run別gate・cleanupを記録している。

| 順 | 条件 / seed | 完全なrun ID | public config | raw / terminal metadata | status | logical calls / HTTP attempts | 比較適格 |
| ---: | --- | --- | --- | --- | --- | ---: | --- |
| 1 | A / 7301 | `warning-retention-study-v1-20260910T080000Z-recent-d60-s7301` | [config](../configs/warning_retention_study_v1/warning-retention-study-v1-20260910T080000Z-recent-d60-s7301.json) | [raw](../runs/output_warning-retention-study-v1-20260910T080000Z-recent-d60-s7301/) / [meta](../runs/output_warning-retention-study-v1-20260910T080000Z-recent-d60-s7301/run_meta.json) | completed | 2,880 / 2,880 | eligible |
| 2 | B / 7301 | `warning-retention-study-v1-20260910T080000Z-retained-d60-s7301` | [config](../configs/warning_retention_study_v1/warning-retention-study-v1-20260910T080000Z-retained-d60-s7301.json) | [raw](../runs/output_warning-retention-study-v1-20260910T080000Z-retained-d60-s7301/) / [meta](../runs/output_warning-retention-study-v1-20260910T080000Z-retained-d60-s7301/run_meta.json) | completed | 2,880 / 2,880 | eligible |
| 3 | B / 7302 | `warning-retention-study-v1-20260910T080000Z-retained-d60-s7302` | [config](../configs/warning_retention_study_v1/warning-retention-study-v1-20260910T080000Z-retained-d60-s7302.json) | [raw](../runs/output_warning-retention-study-v1-20260910T080000Z-retained-d60-s7302/) / [meta](../runs/output_warning-retention-study-v1-20260910T080000Z-retained-d60-s7302/run_meta.json) | completed | 2,880 / 2,880 | eligible |
| 4 | A / 7302 | `warning-retention-study-v1-20260910T080000Z-recent-d60-s7302` | [config](../configs/warning_retention_study_v1/warning-retention-study-v1-20260910T080000Z-recent-d60-s7302.json) | [raw](../runs/output_warning-retention-study-v1-20260910T080000Z-recent-d60-s7302/) / [meta](../runs/output_warning-retention-study-v1-20260910T080000Z-recent-d60-s7302/run_meta.json) | completed | 2,880 / 2,880 | eligible |
| 5 | A / 7303 | `warning-retention-study-v1-20260910T080000Z-recent-d60-s7303` | [config](../configs/warning_retention_study_v1/warning-retention-study-v1-20260910T080000Z-recent-d60-s7303.json) | [raw](../runs/output_warning-retention-study-v1-20260910T080000Z-recent-d60-s7303/) / [meta](../runs/output_warning-retention-study-v1-20260910T080000Z-recent-d60-s7303/run_meta.json) | completed | 2,880 / 2,880 | eligible |
| 6 | B / 7303 | `warning-retention-study-v1-20260910T080000Z-retained-d60-s7303` | [config](../configs/warning_retention_study_v1/warning-retention-study-v1-20260910T080000Z-retained-d60-s7303.json) | [raw](../runs/output_warning-retention-study-v1-20260910T080000Z-retained-d60-s7303/) / [meta](../runs/output_warning-retention-study-v1-20260910T080000Z-retained-d60-s7303/run_meta.json) | completed | 2,880 / 2,880 | eligible |

計画は1runあたり2,880 calls、6run合計17,280 calls、別のengineering schema probe 9 requestsを
合わせ上限17,289 HTTP attempts、4 GPUs・wall-time上限10,800秒。実測合計は本体17,280、
probe9、計17,289 HTTP attemptsで、1,917.576秒だった。
probeは本体の行動観測に含めない。
各runの60 steps・24 agents、2 phasesの実入力coverage、terminal status、source/config同一性、
raw manifest、failure/retry counters、strict validation、publication scanは全6runの完了gateを通過した。
generation retry、transport failure、syntax parse attempt failure、syntax parse failure、schema validation
failureは各runとも0。config/source照合、decoded publication boundaryも全6runで通過した。
今回は中断・失敗・未開始runはなかった。exit codeだけで完走とせず、上記のgateを確認した。
解析でも全6 run・全3組が比較適格となり、各組の24体の初期位置・モデル割当が一致した。

strict検証はvalidだが、各runに5件の検証不能項目を残した。matplotlibのversion未取得と、
それに伴うdependency一覧の不完全性、すべてのprimary Phase/message行にはglobal event IDが
ないこと、raw manifestに外部署名がないこと、公開境界のためruntime endpoint addressを
artifactへ含めずaddress identityを外部検証できないこと、である。これらを検証済みとは呼ばない。

## 直接観測から導出する主結果

公式項目の提示頻度の分母は、直接受信者6体×steps 10–60の306 agent-phases（各Phase別）。
非直接受信者は18体であり、配送と受信者自身の後続step再使用を区別する。

| 条件 / seed | P1公式提示 / 306 | P3公式提示 / 306 | 発行step P1→P3欠落 / 6 | 非直接受信者へのID配送先 / 18 | 同配送edge数 | 同後続reuse人数 / 18 | 同reuse出力数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A / 7301 | 17 | 11 | 2 | 0 | 0 | 0 | 0 |
| B / 7301 | 306 | 306 | 0 | 0 | 0 | 0 | 0 |
| A / 7302 | 61 | 56 | 1 | 0 | 0 | 0 | 0 |
| B / 7302 | 306 | 306 | 0 | 0 | 0 | 0 | 0 |
| A / 7303 | 8 | 2 | 4 | 0 | 0 | 0 | 0 |
| B / 7303 | 306 | 306 | 0 | 0 | 0 | 0 | 0 |

全6 runで、prompt全体のexact-ID有無の件数は上記の公式項目選択件数と一致した。
今回、公式項目を選択していないPhaseで、peerや記憶欄を通じてIDがpromptに残る例は観測されなかった。
全24体の全Phase 1発話でもexact-ID生成は0なので、「生成したが配送先がいなかった」出力も0である。
公式受信は各runで6件のままであり、保持による受信イベントの追加はない。

Aでも欠落時点は一様ではない。seed 7302のagent 1はstep 60まで両Phaseで公式項目が選択され、
この個体では押出しが起きなかった。Bでは全直接受信者の全対象Phaseで保持された。

| seed | B−A: P1公式提示 | B−A: P3公式提示 | B−A: 非直接受信者へのID配送人数 | B−A: 同後続reuse人数 | 初期位置・モデル割当 |
| --- | ---: | ---: | ---: | ---: | --- |
| 7301 | +289 | +295 | +0 | +0 | 一致 |
| 7302 | +245 | +250 | +0 | +0 | 一致 |
| 7303 | +298 | +304 | +0 | +0 | 一致 |

上記は[summaryのprimaryとpairs](../derived/warning-retention-study-metric-v1.0.0_20260910T104224Z/summary.json)に対応する。24体や60 stepsを独立標本として有意差を計算しない。

## 副次結果：移動・避難所と出力の固定分類

| 条件 / seed | 初回到達人数 / 24 | step 60在所人数 / 24 | 平均終点距離 | 危険区域agent-steps | IDなし固定語彙flag出力数 |
| --- | ---: | ---: | ---: | ---: | ---: |
| A / 7301 | 1 | 0 | 22.9583 | 676 | 1398 |
| B / 7301 | 1 | 0 | 19.3750 | 676 | 1385 |
| A / 7302 | 1 | 1 | 18.3750 | 700 | 1393 |
| B / 7302 | 0 | 0 | 20.7917 | 668 | 1363 |
| A / 7303 | 1 | 0 | 25.3750 | 796 | 1359 |
| B / 7303 | 1 | 0 | 24.5417 | 796 | 1374 |

距離は最寄りの避難所矩形内cellまでのManhattan距離。危険区域滞在はsteps 1–60の移動適用後
snapshotから導く。未到達個体の初回到達stepはnullとし、step 60で右打切りとして記録する。

平均終点距離のB−Aはseed順に−3.5833、+2.4167、−0.8333 cells、到達人数差は0、−1、0、
危険区域agent-steps差は0、−32、0だった。行動指標の変化は一方向にそろわない。

全到達例は以下の5 agent-runである。到達だけで警報使用とは認定しない。

| 条件 / seed | agent | 初回到達step | step 60の避難所 |
| --- | ---: | ---: | --- |
| A / 7301 | 5 | 47 | なし |
| B / 7301 | 20 | 4 | なし |
| A / 7302 | 23 | 51 | refuge-east |
| A / 7303 | 14 | 3 | なし |
| B / 7303 | 14 | 3 | なし |

5例中3例は警報発行step 10より前に到達した。seed 7301のA/Bでは警報前から軌跡が異なっており、
初期状態とtemperature 0が同じでも生成と軌跡の一致を保証しない。避難所の形状は全Phaseの環境入力に
含まれるため、これらの到達や、観測された移動差すべてを警報保持の因果効果とは扱わない。

既存disaster metric v2の固定surface-fact分類も保持した。受信後出力306件/runのうち
hazard IDの文字列がmatchした出力は次のとおり。

| 条件 / seed | hazard ID match / 306 |
| --- | ---: |
| A / 7301 | 1 |
| B / 7301 | 0 |
| A / 7302 | 0 |
| B / 7302 | 1 |
| A / 7303 | 0 |
| B / 7303 | 9 |

警報IDが0でも、警報関連文字列への言及がすべて0ではない。これらのmatchや固定英語語彙flagは、
意味的再使用、伝達経路、事実の正確性を認定しない。`unrecognized`も誤情報を意味しない。
hazard/refuge語彙flagは全1,440発話/runを対象とする広い検出で、警報前や共有環境に由来する
発話も含む。結果に合わせた辞書追加や新規LLM採点は行わなかった。
全発話・固定分類・raw行参照をHTMLとsidecarに保存しており、結果に都合のよい例だけを選別していない。

## 解釈と残る制約

機械的な操作確認として、Bは公式警報の継続提示を達成した。しかし、この3組ではexact-ID生成・
配送・後続reuseはA/Bとも0のままだった。「入力から消えていた」ことだけでは、Bでも観測された
exact-ID伝達の不足を説明できない。注意・理解・意図や言い換え全般については判定していない。

3 seeds、固定3モデル・各8体、1種類の警報、固定6受信者、60 stepsの探索的比較である。
24体を24回の独立反復と数えない。保持順序、peerの1枠置換、token長の違いを分離できず、
警報前の生成にもA/B差があり、行動差の介入への帰属には限界がある。
一般的な記憶能力、内部認知や現実の災害対応能力の評価ではない。
modelの`reasoning`は出力説明欄であり、内部思考へのアクセスではない。

提案（未実施）として、警報IDを落とした言い換えの検出を、別の事前固定した注釈基準で検証する
余地がある。今回の結果や判定規則を変更するものではなく、追加runは実行していない。

## 実装・検証・追跡可能性

入力選択は[message selection仕様](MESSAGE_SELECTION_V1_SPEC.md)、実request入力保存は
[message presentation仕様](MESSAGE_PRESENTATION_V1_SPEC.md)としてversionを持たせた。
[解析器](../tools/analyze_warning_retention_study.py)は受信・配送の時系列を独立に再生し、
rawに保存された選択項目との一致を照合する。prompt hash・全coverage・元の受信/発話/行動行の
参照を残し、既存の[disaster metric v2](DISASTER_METRIC_V2_SPEC.md)は変更していない。

| 契約 | version / SHA-256 |
| --- | --- |
| 実験protocol | `warning-retention-study-v1.0.0` |
| 新規metric | `warning-retention-study-metric-v1.0.0` |
| 新規metric仕様SHA-256 | `a8bca6c4b0f9563e3d2a7bfaeabd2b9530eec28abf86495360fafb60a8452e81` |
| 既存warning metric | `disaster-metric-v2.0.0` |
| 既存warning metric仕様SHA-256 | `cd0ca4fec67d945a2b878fe5c0c7e49391e9bacf10a3f7aa0a142f7962cbf711` |
| log schema / input log | `2.0.0` / `message-presentation-v1.0.0` |
| prompt / response | `bounded-prompts-v3.0.0` / `phase-response-v3.0.0` |
| transport | `single-generation-strict-json-no-redirect-v3.0.0` |
| inference source | `a5b0e68f6c4756ad1695b2898eff870f9d89cdda` |
| 解析source | `789827d47df5a93b5528e0a04f043763fcdb7da2`（clean） |
| 解析artifact | `derived/warning-retention-study-metric-v1.0.0_20260910T104224Z` |

推論開始前のローカル全suiteは449 passed・1 skipped・171 subtests。skipはWindowsのsymlink作成
権限によるものだった。phase barrier、通信境界、run衝突、中断、応答契約、公開境界、入力選択・
prompt再構成・欠落・重複・hash破損・後続step判定などを回帰検証した。
6条件の模擬応答によるend-to-end解析も通過したが、これは工学検証であり、実験結果には含めない。

実データでは、全6 runのstrict validation、全3組の初期状態一致、17,280 Phase入力のcoverage、
69ファイルの転送・取り込み前後のバイト同一性を確認した。既存108ファイルも変更されていない。
解析は生ログを含む上記clean commitから生成し、推論sourceに対する実装・仕様・configの一致を照合した。
独立したread-only再計算でも全6 runの指定指標と全3組の差が一致し、raw・config・summaryを変更していない。

HTMLは生成前のDOM検証で24体の選択、step変更、観測欄、未完了条件のnull表示、canvas描画呼出しを
確認した。実データHTMLでは全6 run・3 seeds、全agent/stepの入力とsidecarの一致、manifest hash、
実行JavaScriptと固定rendererの一致を確認した。実ブラウザーのローカルHTML閲覧はBrowser URL security
policyにより拒否され、視覚的な表示確認は未実施。別の閲覧方法で拒否を迂回していない。

主な変更ファイルは `engine/message_selection.py`、`engine/agent.py`、`engine/sim.py`、
`engine/provenance.py`、`engine/config.py`、`tools/validate_run.py`、4個のstudy生成・実行・解析・描画tool、
公開configとmanifest、3仕様書とprotocol、回帰tests、CI、README、および本報告と全生ログ・導出物。
実際の変更一覧は推論source commitのdiffで追跡できる。

証拠への入口:

- [全結果](../derived/warning-retention-study-metric-v1.0.0_20260910T104224Z/summary.json): 6 runの適格性、config/source/raw hashes、3組の対応差。
- [A/B比較HTMLを開く](https://suii00.github.io/mixed-cognition-sim-research/derived/warning-retention-study-metric-v1.0.0_20260910T104224Z/comparison.html): seed・24体・step選択、受信→提示→発話→配送→行動と地図。[保存済みHTML](../derived/warning-retention-study-metric-v1.0.0_20260910T104224Z/comparison.html)からのダウンロードも可能。
- [Phase入力](../derived/warning-retention-study-metric-v1.0.0_20260910T104224Z/agent_phases.jsonl): 全実入力提示とraw行参照。
- [全発話](../derived/warning-retention-study-metric-v1.0.0_20260910T104224Z/speech.jsonl): Phase 1発話・配送先・固定語彙flag。
- [個体集計](../derived/warning-retention-study-metric-v1.0.0_20260910T104224Z/agents.jsonl): 初回到達・打切り・終点。
- [位置推移](../derived/warning-retention-study-metric-v1.0.0_20260910T104224Z/agent_steps.jsonl): 全agent/stepの幾何指標。
- [警報出力分類](../derived/warning-retention-study-metric-v1.0.0_20260910T104224Z/warning_outputs.jsonl): 既存v2の受信後出力・後続reuse・固定事実分類。
- [解析来歴](../derived/warning-retention-study-metric-v1.0.0_20260910T104224Z/analysis_meta.json): clean解析source、推論source、仕様・実装・入力hash。
- [生成物manifest](../derived/warning-retention-study-metric-v1.0.0_20260910T104224Z/derived_manifest.json): 導出ファイルのSHA-256とbytes。
- [実行検証証拠](../derived/validation-warning-retention-study-v1-20260910T080000Z/verification.json): probe・run・terminal・cleanup・公開境界の検証。
- [実行証拠manifest](../derived/validation-warning-retention-study-v1-20260910T080000Z/artifact_manifest.json): 実行検証とprobe生ログのhash。

実行・検証コマンド（repository root、上記sourceで実施）:

```bash
python tools/build_warning_retention_study.py --check
python tools/run_warning_retention_study.py --contract-only
python -m pytest -q
python tools/run_warning_retention_study.py --source-git-sha a5b0e68f6c4756ad1695b2898eff870f9d89cdda --gpu-indices <four-runtime-indices>
python tools/analyze_warning_retention_study.py --manifest configs/warning_retention_study_v1/manifest.json --runs-root runs --output-dir derived/warning-retention-study-metric-v1.0.0_20260910T104224Z --metric-spec-sha256 a8bca6c4b0f9563e3d2a7bfaeabd2b9530eec28abf86495360fafb60a8452e81
python tools/verify_repository.py
python tools/scan_publication.py . --git-history
```

実行・解析の出力先は一度だけ作成し、同名の再実行は衝突として拒否する。再現解析では未使用の
version付きtimestamp directoryを指定する。rawの編集・上書き・再実行による追記はしない。
runtime address、device固有ID、credentialは公開config・生ログへコピーせず、受領後も内容を変換していない。
