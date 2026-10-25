# Tools, artifacts and sources task brief

Read the workbench design. Own miniagent/programming.py, new miniagent/artifacts.py, miniagent/search.py and focused tests. Do not edit agent/model/tools/web/config/frontend.

## Tool management

- Existing manifests continue to load. Add enabled=True, optional input_schema=None and tests=[] defaults. tests has up to 20 {arguments: object, expected_result: JSON} cases, in addition to the existing required example. Schema is an explicit supported subset of object JSON Schema; validate it and actual tool inputs; unsupported rules must not silently be claimed enforced. Preserve legacy arguments wrapper.
- Summaries expose enabled, version, previous_version (number or None), test_count and input_schema/tests along with existing fields. load_tools excludes disabled tools. Preserve old manifest before successful replacement under local_tools/.history/; only publish a new manifest after all tests pass. A failed update leaves current and previous working versions untouched.
- Public ProgrammingTools methods set_enabled(name: str, enabled: bool)->dict, rollback(name: str)->dict, test_saved(name: str)->dict. rollback restores the retained previous code/tests/schema without blindly rerunning code; save current as previous so it is reversible. test_saved returns {passed: bool,total: int,results:[{index:int,passed:bool,error?:str}]}; bound/private-data-safe error strings. Existing Python limits apply. Mutations are serialized with existing lock. Document actual test count.

## Artifacts

- ArtifactStore(root: Path, workspace: Path), scan()->dict, capture(before: dict, *, run_id: str)->list[dict], get(artifact_id: str)->tuple[dict,bytes], preview(artifact_id: str)->dict.
- Scan supported UTF-8 text/code files in workspace (txt/md/json/csv/py/js/ts/tsx/jsx/css/html/yaml/yml), bounded traversal, exclude hidden paths/symlinks. Archive created/modified files in data/artifacts; max 2 MiB per file, max 50 captured per call. Stable random hex artifact IDs, immutable bytes and JSON metadata. Do not expose arbitrary file paths or execute markup. Path/symlink/ID validation must be tested.
- Metadata: {id,name,size,change:'created'|'modified',created_at,run_id,download_url:'/api/artifacts/<id>/download',preview_url:'/api/artifacts/<id>'}. name is workspace-relative. Optional bounded diff for modified text; snapshot may retain bounded old text. preview returns {artifact:metadata,content:string,truncated:bool,diff?:string,columns?:string[],rows?:string[][]}; CSV use stdlib csv for first 30 rows. get returns archived content; changes to workspace must not change previous download bytes. Invalid/unsafe requests raise ValueError; missing artifact FileNotFoundError.

## Sources

- Preserve search tool output shape; add source_id stable for URL (e.g. S plus short hash), source title/url/date/read status to existing results/fetch output, all within current output bounds. Give model instructions to cite [S...](actual URL) when using returned sources; never create unobserved URLs.
- Fetch cache is per execution_run_id() from miniagent.tools (runner owner adds it). Repeated same URL/offset within one run may reuse raw fetched content, label cached=True; no cache across runs. No key/content leakage. Existing tests calling methods without run context must remain supported; then do not cache.

Report code changes, test commands/results and limitations to root. Do not commit, publish or spawn subagents.
