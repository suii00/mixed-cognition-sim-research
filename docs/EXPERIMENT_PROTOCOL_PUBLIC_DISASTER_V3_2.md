# Public disaster formal protocol v3.2.0

## Status

この文書はv3.2.0の最初のmodel outputを生成する前に固定するprospective amendmentである。
`EXPERIMENT_PROTOCOL_PUBLIC_DISASTER_V3_1.md`の観測連鎖、介入、対照、seed、world、prompt、model、
context、sampling temperature、response contract、log schema、metric、60-run matrixを継承する。

Operational attemptは12 runの個別検証後、Llama responseが1,024-token ceilingへ到達したため
matrix完了前にfail closedした。public treeへのpromoteは0であり、v3.2.0 attemptをformal resultへ
集計しない。追加実行または条件変更には別のprospective protocolと承認を要求する。

## Single declared change

v3.1.0 operational attemptで、Llama Phase 3 responseがHTTP 200を返しながら
`finish_reason=length`、completion 512 tokensで終了し、開いたJSON objectが閉じない直接観測が
一件あった。model output本文の意味内容やsimulation結果は変更判断に用いていない。

v3.2.0では`llm_defaults.max_tokens`だけを512から1,024へ変更する。prompt、JSON schema、desired
behavior、temperature、contextは変更しない。有限のceilingであるため成功を事前には主張しない。

- protocol version: `formal-public-disaster-protocol-v3.2.0`
- matrix schema: `formal-public-disaster-matrix-v1.2.0`
- run ID prefix: `public-disaster-v3p2-`
- config directory: `configs/public_formal_disaster_v3_2/`
- max tokens per HTTP attempt: 1,024
- context: 4,096
- planned runs: 60
- planned logical calls / HTTP attempts: 144,000 / 144,000
- retry contingency: 0
- seeds: 3101--3105
- maximum GPUs: 6
- wall-time launcher ceiling: 8 hours

max-token ceilingが異なるため、v3.0.0およびv3.1.0 attemptをv3.2.0 formal resultの反復として
集計しない。両attemptはoperational incident evidenceとして別に保持する。

## Validation and publication gate

`public-strict-gate-v1.1.0`を使用する。strict validation errorはfail closedとし、artifactだけから
証明不能な`unverifiable`事項は件数とcanonical digestを記録する。retry、transport、syntax、
schema failure、publication finding、runtime-binding byte、server log、GPU scope escape、cleanup
failureはいずれも一件で全matrixを停止する。全60 runがPASSするまでpublic `runs/`へ一件も
promoteしない。redactionやsanitizationは行わない。
