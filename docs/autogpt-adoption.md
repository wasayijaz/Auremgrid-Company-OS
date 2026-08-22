# AutoGPT adoption decision

Auremgrid reviewed AutoGPT as a source of ideas for its agent and Intelligence
surfaces. The decision is to adopt selected patterns as native, dependency-free
contracts—not to embed or vendor the AutoGPT platform.

## License and product boundary

The modern `autogpt_platform` repository is licensed under Polyform Shield. It
is therefore not a suitable foundation for building or hosting a competing
agency operating product. AutoGPT Classic, Forge, and the benchmark material
are MIT-licensed, but Classic is experimental/unsupported and the Agent
Protocol is archived. These facts make them useful references, not runtime
dependencies or supported extension points.

No AutoGPT source, platform package, agent prompt, or protocol implementation
is copied into this repository. The evaluation command is an Auremgrid-native
implementation and runs without network access or additional dependencies.

## Concepts adopted

- A bounded evaluation suite with named scenarios and pass/fail output,
  inspired by benchmark-style regression checks rather than model leaderboards.
- Explicit agent/intelligence contracts: evidence citations, confidence and
  uncertainty, structured hypotheses/options/scenarios/recommendation/dissent,
  and auditable proposed actions.
- Separation between reasoning and execution. Auremgrid returns approval and
  workflow descriptors; it does not let an evaluation or model call execute a
  write.
- Deterministic fallback as a first-class result when an optional provider is
  absent or malformed.

Run the built-in suite with:

```text
python -m auremgrid.cli evaluate-intelligence
```

The command exits nonzero if any scenario fails and prints a JSON report that
can be retained as release evidence.

## Concepts rejected

- AutoGPT Platform as an embedded hosted-agent runtime, because its Polyform
  Shield license and platform scope are not an appropriate product foundation.
- AutoGPT Classic/Forge runtime coupling, because Classic is experimental and
  unsupported and Forge would introduce a second agent runtime and source of
  truth.
- Archived Agent Protocol integration, network benchmark dependencies, and
  copied benchmark fixtures as production behavior.
- Autonomous tool execution, hidden memory, or model-generated canonical
  records. All Auremgrid writes remain behind normal ACL, workflow, and human
  approval boundaries.

