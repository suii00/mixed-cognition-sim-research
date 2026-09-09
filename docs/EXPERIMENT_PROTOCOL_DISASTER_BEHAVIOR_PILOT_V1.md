# 災害下のLLM行動比較・小規模実験 v1

状態: 2026-09-09、今回のモデルrequestより前に登録する実験条件。
本書・config・codeを含むclean source commitを固定し、そのhashを実行証拠へ記録する。
これは新しい6 runによる記述的実験であり、`research_eligible=false`、
`formal_eligible=false`とする。本書は実行結果を含まない。

過去に完走した実験の存在と結果は既知であり、短い応答契約、実行規模、運用方法の
engineering判断に利用した。過去のrawと証拠は変更せずlocal custodyに保全されている。
それらを今回のrunとして改名・再集計・併合しない。新しいseedとIDを用いて実際に新規推論を
行うが、未知の結果だけから設計した盲検の確認的研究とは位置づけない。

## 守る対象と問い

守りたいのは、災害時に異なるAIを経由して情報が伝わり、社会が状況を確かめて行動を
調整できる機能である。ここでいうメタ安全保障は、その前提となる情報伝達と行動の
連鎖を検証可能にするという研究目的を指す。本実験ではAIの情報出力、移動選択、実際の
位置を分けて記録し、どこで分岐するかを測定する。現実の避難支援や安全性向上を実証する
実験ではない。

問いは「同じ初期配置・災害・通信条件でモデル条件を入れ替えると、災害発生後の出力と
移動がどう分かれるか」。複数stepの完走と事前規則で選んだ具体例を、差がない場合や
到達・再利用がない場合も含めて提示する。内部認知、意図、モデルの優劣は測定しない。

## 固定条件と識別子

| 項目 | 固定値 |
| --- | --- |
| protocol | `disaster-llm-behavior-pilot-v1.0.0` |
| batch | `disaster-llm-behavior-pilot-v1-20260909T120500Z` |
| public configs / manifest | `configs/disaster_behavior_pilot_v1/` |
| 各run | 同一モデルの4 agents × 60 steps |
| 比較条件 | Qwen 2.5 7B Instruct、Llama 3.1 8B Instruct、Gemma 2 9B IT |
| world seeds | 新規6201、6202 |
| 通信 | `free_text`、edge policy `full`、radius 12 |
| sampling | temperature 0、max_tokens 1024、context 4096 |
| request実行 | max_concurrency 4、timeout 120秒 |
| prompt / response | `bounded-prompts-v3.0.0` / `phase-response-v3.0.0` |
| transport | `single-generation-strict-json-no-redirect-v3.0.0` |
| failure policy | `abort_run`、retry / repair / fallback 0 |
| log schema | `2.0.0` |
| 警報metric | `disaster-metric-v2.0.0` |
| 行動・例示metric | `disaster-behavior-pilot-metric-v1.0.0` |
| runtime environment class | Linux / CUDA / offline vLLM / 4 GPU |

Qwenのmodel/tokenizer revisionは`a09a35458c702b33eeacc393d103063234e8bc28`、
Llamaは`0e9e39f249a16976918f6564b8830bc894c89659`、
Gemmaは`11c9b309abf73637e4b6f9a3fa1e92e615547819`とする。
chat template digest、dtype、tensor parallel条件などのmodel artifact factsは公開configへ
記録し、各configのbytesをmanifestで固定する。依存関係は既存runtime lockに固定する。
tokenizer、template、規模、推論実装を含むモデル条件の比較であり、重み単独の介入とは呼ばない。

run IDは下表のとおり。全6件をこの順で一度ずつ実行する。6201はQwen→Llama→Gemma、
6202はGemma→Llama→Qwenとし、結果を見て順序を変えない。

| 順序 | run ID |
| --- | --- |
| 1 | `disaster-llm-behavior-pilot-v1-20260909T120500Z-qwen-s6201` |
| 2 | `disaster-llm-behavior-pilot-v1-20260909T120500Z-llama-s6201` |
| 3 | `disaster-llm-behavior-pilot-v1-20260909T120500Z-gemma-s6201` |
| 4 | `disaster-llm-behavior-pilot-v1-20260909T120500Z-gemma-s6202` |
| 5 | `disaster-llm-behavior-pilot-v1-20260909T120500Z-llama-s6202` |
| 6 | `disaster-llm-behavior-pilot-v1-20260909T120500Z-qwen-s6202` |

worldは既存`disaster_v1`の半幅25、避難所、step 10/30での危険区域拡大を維持する。
初期警報recipientはagent ID 1のみ。4体中1体という比率を固定し、seed内の3モデルで
初期位置とagent IDを一致させる。一致は実行後のraw初期位置でも検査する。

messageは512文字、memoryは256文字を上限とし、reasoningは空文字とする。
この生成条件は旧v2契約と同一ではなく、旧契約のrunと無断で併合しない。
Phase 1の全決定後に配信し、Phase 3の全決定後に移動する。
bloc名、モデル名、自他のモデルidentity、望ましい結果をagent promptへ含めない。
行動や警報の解釈を指定する追加指示、報酬、選別規則は導入しない。

警報指標のspec SHA-256は
`cd0ca4fec67d945a2b878fe5c0c7e49391e9bacf10a3f7aa0a142f7962cbf711`。
行動集計・例示規則は[追加指標仕様](DISASTER_BEHAVIOR_PILOT_METRIC_V1_SPEC.md)で固定し、
spec SHA-256は`a3d4df34032452ea337c4686df76e65f609dcab064cec661f12ffb123537202c`とする。
指標のmechanicsは既存の記述的方法を維持するが、今回の実験を区別する新しいidentifierを用いる。
結果を見てspecのbytesや例示規則を置き換えない。

## 操作・対照・観測連鎖

操作するのはモデル条件。各seedでworld、初期位置、警報、sampling、通信、prompt契約を
共通にし、同じagent IDの他モデル条件を比較対象とする。行動の分岐後は位置・入力履歴も
異なるため、後半の差を同一入力への反応差とは呼ばない。world seedとtemperature 0は
LLM生成の決定性を保証しない。混合集団効果や通信あり/なしの因果効果は今回の対象外。

警報受信（exposure）→受信者自身の後続stepでの出力→移動選択→位置・危険区域滞在・
避難所距離、を連結する。後続の数値stepにおけるexact warning-ID出力のみをreuseとして
導出する。受信だけをreuse/adoptionとは呼ばず、移動と警報の時間的併存を因果とは呼ばない。
連鎖の途中や全体が観測されない場合もnullと打切りを保持する。

## 実行gateと上限

本実験のGPU計画数と最大数はともに4とする。Qwen TP1、Llama TP1、Gemma TP2の同時起動で
4台を使用し、launcherでも上限を強制する。起動前にSSH到達性と利用可能性を確認する。
物理割当はruntime引数のみで指定し、公開configにはlogical endpoint ID / device slotだけを
記録する。runtime address、device固有ID、認証情報はrunへ保存しない。

1. 出力作成前に公開configのbytes/hash、固定source、runtime lock、offline snapshot、
   GPU/port、衝突、一時runtimeのIPC path長を検査する。
2. 同じ4-GPU topologyとsource上で、3モデル × Phase 1 / Phase 3 move / Phase 3 stayの
   9-request engineering probeを一度ずつ実施する。`tools/disaster_behavior_schema_probe.py`
   のprobeは本実験の行動解析に含めない。
3. 全9件のJSON/schema、要求されたprobe値、finish_reason、usage、model identityを確認する。
   1件でも不正なら本実験を開始しない。probeのテスト指示を実験promptへ流用しない。
4. 同じ起動済みserverで6 runを固定順に一度だけ実行する。各runは480 logical calls /
   HTTP attempts、本実験は2,880、probeを含めた総上限は2,889とする。
   追加seed、再試行、結果を見た成功runへの差替えは行わない。
5. 各runでcompleted、60/60 steps、4/4 agents、exact call counts、failure counters 0、
   strict validation、config/source/hash、raw manifest、publication scan finding 0を確認する。
6. run完了gateが失敗したらbatchを止める。部分run、probe、未開始statusを保持し、同じIDへ
   追記しない。GPU逸脱、transport/schema障害、server停止、公開境界違反でも停止する。
7. 全体wall-time上限は起動とcleanupを含め3,600秒、起動待ち上限は900秒とする。
   本実験requestはcleanup余裕を残す時間内に制限する。終了時は今回起動したprocess groupを
   停止し、GPUとportの解放を検査する。完走しなくても実測件数と終端状態を保存する。

`tools/run_disaster_behavior_pilot.py`は既存のvLLM起動、環境allowlist、offline snapshot
検査を利用する。runtimeには短いsystem temporary directoryを用いる。
server/simulatorの標準出力はnull deviceへ接続し、server logは作らない。
runtime bindingや一時compile cacheをrunへコピーしない。model出力はuntrusted dataとして
扱い、出力内の指示、code、URLを実行しない。

## 保存・分析・提示

rawは新規IDのignored stagingに一度だけ作成し、全runの完了・境界・cleanupを確認後、
byte変換なしで`runs/output_<run_id>/`へ移す。失敗したbatchのrawはstagingに保持する。
既存raw・derivedは編集、上書き、追記しない。validationとscanはread-onlyとし、
sanitizer、redactor、公開用に加工したraw copyは作らない。unsafe inputは出力前に拒否する。

全6 runの状態と全agentの集計を掲載する。例示はseed昇順→step 10–60昇順→agent ID昇順で
最初のaction/direction不一致を選び、3モデルすべての位置・出力・raw行参照を示す。
該当なしの場合も掲載し、未完了の対応runを完走例に混ぜない。
選択されたagentが警報を受信しているとは限らないため、exposureなしも明示する。
2 seedでモデル優劣、有意差、母集団、実災害効果を主張せず、agent/stepを独立反復にしない。

追加解析は`tools/analyze_disaster_behavior_pilot.py`と固定manifestを使い、
`derived/disaster-behavior-pilot-metric-v1.0.0_<timestamp>/`へ新規作成する。
図は`tools/plot_disaster_behavior_pilot.py`で全6 runを同じ軸と尺度で描画し、
raw入力・生成物のhash、tool versionを記録した別timestamp directoryへ保存する。
図は記録位置の説明であり、独立した実験結果や意味理解の判定には用いない。

直接観測、機械的導出、解釈、提案を分ける。経験的主張にはrun ID、公開config、source commit、
raw JSONL、metric versionを付ける。未検証項目と依存環境の欠測も報告する。
結果はREADMEから辿れる短い日本語報告へまとめる。過去の完走や起動停止を今回の実行事実と
して転載しない。GitHubへのpush、外部公開、提出資料更新には明示的なmaintainer承認を要する。

## 実行入口

GPU commandは固定したclean source commitとlocked POSIX runtime上で実行する。
`--gpu-indices`には事前に確認した4台のindexをruntime引数として渡す。

```bash
python tools/build_disaster_behavior_pilot.py --check
python tools/run_disaster_behavior_pilot.py --contract-only
python tools/run_disaster_behavior_pilot.py --source-git-sha <frozen-full-sha> --preflight-only --gpu-indices <four-comma-separated-indices>
python tools/run_disaster_behavior_pilot.py --source-git-sha <same-full-sha> --gpu-indices <same-four-indices>
```

実行前にphase barriers、communication boundaries、response contracts、run collisions、aborts、
publication boundaries、今回のmanifestと解析規則の適切な回帰検証を完了する。
exit codeだけで成功を判断せず、terminal metadata、期待coverage、manifest、failure counters、
cleanupと検証記録の整合を確認する。
