# Public disaster formal protocol v3.1.0

## Status

この文書はv3.1.0の最初のmodel outputを生成する前に固定するprospective amendmentである。
`EXPERIMENT_PROTOCOL_PUBLIC_DISASTER_V3.md`の観測連鎖、介入、対照、seed、world、prompt、model、
context、sampling temperature、response contract、log schema、metric、60-run matrixを継承する。

Operational attemptは51 runの個別検証後、Llama responseが512-token ceilingへ到達したため
matrix完了前にfail closedした。public treeへのpromoteは0であり、v3.1.0 attemptをformal resultへ
集計しない。このprotocolはv3.2.0へsupersedeされた。

## Single declared change

v3.0.0のoperational attemptで、Llama Phase 3 responseがHTTP 200を返しながら
`finish_reason=length`、completion 256 tokensで終了し、開いたJSON objectが閉じない直接観測が
一件あった。model output本文やsimulation結果の内容は変更判断に用いていない。

v3.1.0では`llm_defaults.max_tokens`だけを256から512へ変更する。これは既存の三モデルengineering
smokeと同じ上限である。prompt、JSON schema、desired behavior、temperatureは変更しない。

- protocol version: `formal-public-disaster-protocol-v3.1.0`
- matrix schema: `formal-public-disaster-matrix-v1.1.0`
- run ID prefix: `public-disaster-v3p1-`
- config directory: `configs/public_formal_disaster_v3_1/`
- max tokens per HTTP attempt: 512
- context: 4096
- planned runs: 60
- planned logical calls / HTTP attempts: 144,000 / 144,000
- retry contingency: 0
- seeds: 3101--3105
- maximum GPUs: 6
- wall-time launcher ceiling: 8 hours

max-token ceilingが異なるため、v3.0.0 attemptをv3.1.0 formal resultの反復として集計しない。
v3.0.0 attemptはoperational incident evidenceとして別に保持する。

## Validation and publication gate

v3.0.0で修正した`public-strict-gate-v1.1.0`を使用する。strict validation errorはfail closedとし、
artifactだけから証明不能な`unverifiable`事項は件数とcanonical digestを記録する。retry、transport、
syntax、schema failure、publication finding、runtime-binding byte、server log、GPU scope escape、
cleanup failureはいずれも一件で全matrixを停止する。全60 runがPASSするまでpublic `runs/`へ
一件もpromoteしない。redactionやsanitizationは行わない。
