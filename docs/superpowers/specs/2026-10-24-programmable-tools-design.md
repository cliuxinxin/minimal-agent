# Programmable tools and web search

## Intent

Let the owner ask the desktop Agent to write and test Python, turn useful code into persistent tools, and research current web information. Keep the standard OpenAI and Anthropic adapters, local configuration, and simple double-click launch.

## Decisions

- Replace `calculate` with `run_python` and `create_tool`.
- Execute Python in a child process with the workspace as its working directory, a 60-second timeout, a 64 KiB output budget, and cooperative cancellation that terminates the child. This is local code execution with the current user's permissions, not an operating-system sandbox.
- A reusable tool defines `run(arguments)` and must pass its supplied example/expected-result test before atomic registration. Store its source, description, example, and metadata in ignored `local_tools/` JSON files. Refresh tool definitions before each model request so new tools are usable immediately and across sessions.
- Default to 80 model requests per task. Offer 80, 800, and unlimited request count in the settings. Retain stop controls and stop after eight consecutive all-error rounds or eight identical call/result rounds.
- Add official DeepSeek Search and Fetch as ordinary native tools independent of the model provider. An optional search key enables both. Keep the official endpoint fixed and preserve source URLs and pagination.
- Keep the existing four model fields compatible. Add only `max_steps` and optional `search_api_key`. Omitted new fields retain existing values during settings updates. Never return secrets to the frontend.
- Add a desktop tools drawer that shows persistent tools and their source, and a simple search-key and step-limit section in settings. Generated tools and keys survive normal Git updates.

## Approaches

The chosen implementation extends the existing explicit loop and Python standard-library host. A new orchestration framework would add migration and dependencies without helping these features. An arbitrary shell tool would add a broader interface than the requested Python authoring and registration workflow. A separate hosted sandbox can be added later if operating-system isolation is needed.

## Verification

Test execution and persistence with real Python subprocesses; test timeout, stop, output bounds, failed test registration, registry refresh, configuration compatibility, and secret redaction. Test DeepSeek requests with a test-only HTTP substitute using the documented contract. Check the real desktop interface and publish a verified fast-forward commit to the authorized GitHub repository. Live provider validation requires keys entered locally by the owner.
