# Public disaster formal protocol v3.0.0

## Status and scope

この文書は、最初のmodel outputが生成される前に固定するprospective protocolである。
対象は60 step、24 agentのdisaster scenarioで、vLLMだけを標準backendとして使う。
旧protocolのraw outputを変換・補完・選別する実験ではなく、新しいseedで全cellを実行する。

- protocol version: `formal-public-disaster-protocol-v3.0.0`
- metric version: `disaster-metric-v1.0.0`
- response contract: `phase-response-v2.0.0`
- log schema: `2.0.0`
- seeds: `3101`, `3102`, `3103`, `3104`, `3105`
- planned runs: 60
- planned logical LLM calls: 144,000
- planned HTTP attempts: 144,000
- retry contingency: 0
- research eligibility declared prospectively: true

seedは結果を見る前に連続した未使用の5値として固定する。結果に応じたseedの追加、削除、
置換を行わない。失敗後の再実行は、この144,000-call envelopeには含まれず、新しい承認なしに
開始しない。

## Experimental matrix

各compositionを三つのcommunication conditionと五つのseedで実行する。

| Public label | Config composition | Agent allocation |
|---|---|---:|
| QQQ | `qwen_only` | Qwen 24 |
| LLL | `llama_only` | Llama 24 |
| GGG | `gemma_only` | Gemma 24 |
| Three-model mixed | `mixed` | Qwen 8, Llama 8, Gemma 8 |

communication conditionは`free_text`、`structured_warning`、
`communication_none`の三つとする。`communication_none`は1 stepあたり24 call、他の二条件は
Phase 1とPhase 3の合計48 callである。従って1 cellはそれぞれ1,440または2,880 call、
全体は144,000 callとなる。

world、hazard schedule、official warning、sampling、context、token上限、phase barrier、
agent数、step数は、宣言したcompositionとcommunication condition以外で変えない。
temperatureは0.0、max tokensは256、model contextは4096とする。world seedだけを理由に
LLM outputの決定性は主張しない。

## Observed chain, intervention, and control

事前登録する一本の観測連鎖は次の通りである。

1. officialまたはagent messageをrecipientが受信した記録を`exposure`として観測する。
2. exposureより後の別stepでrecipient自身が生成したoutputを観測する。
3. version固定metricがそのoutputにwarning reuseを判定した場合だけ`reuse`として機械的に導出する。
4. 同じagentの後続position、hazard residence、refuge arrivalを時系列で結合する。

communication policyを操作可能な介入点とし、`communication_none`をno-agent-message controlと
する。対象領域はcommunication/reuseとevacuation/movementの二領域である。受信だけをreuse、
adoption、因果効果とは呼ばない。後から選んだ語彙や閾値を過去のeventへ適用しない。

model-generated `reasoning`は説明fieldとして保存するが、model内部の真の推論過程とは扱わない。
単一runや単一引用は例示に限り、頑健性の根拠にしない。null、negative、aborted、矛盾するrunも
選別せず保持する。

## Versioned change from earlier disaster configs

このprotocolは旧legacy response contractを継承しない。公開runをstrict schema validationできる
よう、`phase-response-v2.0.0`とlog schema `2.0.0`を新しいprotocol versionの下で事前に固定する。
promptの意味論、phase order、scenario、報酬、desired behaviorは変更しない。このcontract差を
無視して旧runと新runを同一条件の反復とは扱わない。

## Runtime topology

runtime topologyは実験factorではなく、公開configとは分離したoperational routingである。
最大6 GPUを次の5 server processへ割り当てる。

- Qwen worker A: 1 GPU
- Qwen worker B: 1 GPU
- Llama worker A: 1 GPU
- Llama worker B: 1 GPU
- Gemma shared tensor-parallel server: 2 GPU

Gemmaの二つのlogical endpointは同じloopback serverへrouteする。これは4096 context profileを
24 GiB GPU上で安全に保持するためである。Qwen/Llama replicaとGemma共有のroutingはagent prompt、
public config snapshot、raw JSONLへ入れない。二つのworker laneは各30 run、72,000 callを担当する。
同じcomposition/seedの三communication conditionは同じlogical replicaへ固定し、paired comparisonに
replica差を混入させない。composition blockのworker割当はseedごとに交互に反転する。

## Publication-by-construction execution gate

実行元はcleanなfull Git SHAへ固定し、exact runtime lockとoffline exact model snapshotを起動前に
確認する。vLLM serverとsimulationのstdin/stdout/stderrは直接null deviceへ接続し、server log
fileやpipeを作らない。子process環境はallowlistで再構成し、認証、SSH、telemetry用environmentを
渡さない。serverはloopbackだけにbindする。

各runはignored stagingへ一度だけ作成する。変換やsanitizationは行わない。各run直後と60 run
完了後に次を確認し、全条件がPASSした場合だけ同一bytesを`runs/output_<run_id>/`へmoveする。

- status completed、60/60 step、24/24 agent
- logical callとHTTP attemptがmanifestのexact count
- retry、transport、syntax、schema failureがすべて0
- strict validation PASS。validatorが証明不能と明示する事項は件数とdigestを保存し、
  validation errorと混同しない
- publication scan finding 0
- runtime-binding byteの非残存
- runtime/run内のserver log file 0
- source SHAとclean-state provenanceの一致
- 全process group停止、選択GPU scope内の使用、cleanup後のGPU release

一つでも失敗した場合は全60 runをpublic treeへ昇格しない。失敗runをredactして成功扱いにしない。
stagingに残る安全なcompleted、negative、aborted evidenceは元bytesを保持する。

## Authorized execution envelope

長時間GPU実行の直前に、少なくとも次を明記して承認を得る。

- source Git SHA
- 60 run / 144,000 logical calls / 144,000 HTTP attempts / retry 0
- seeds 3101--3105
- GPU indicesと最大6 GPU
- wall-time hard limit（launcherの上限は8時間）
- remote staging、final run、verification evidenceの出力先
- fail-fast停止条件

承認された上限を超えるoptionはlauncherが拒否する。

## Strict-validation epistemic limits

strict validatorの`valid`は、現行schemaから機械的に検査できる整合条件がすべてPASSしたことを
意味する。外部署名によるcryptographic authenticity、意図的に成果物から除外したruntime address、
全primary rowを覆うglobal event identityなど、成果物だけから証明できない事項は
`unverifiable`として別に報告される。これらをPASSと主張せず、同時にvalidation errorとしても
扱わない。formal gateは`valid == true`を要求し、各runのunverifiable listは本文を複製せず、件数と
canonical digestをaggregate evidenceへ保存する。
