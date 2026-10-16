# VASL AI Bridge

**Play ASL with an AI opponent that can consult your references and return to your game record.**

VASL AI Bridge is a Windows companion for playing Advanced Squad Leader alongside VASL. It keeps selected rules, scenario materials, your conversation and board images available to the AI during play.

**Current AI support:** This version connects through **Sign in with ChatGPT** only. Other AI providers and API-key connections are not currently supported.

The AI chooses and explains its side's actions. You operate VASL: move counters, roll dice, check line of sight, resolve procedures and report the results.

## Features

- **References within reach.** Give the AI your rules, combat charts, scenario sheets and other materials. It can search readable text and inspect pages or images when it needs to check a detail.
- **Continue a game without starting the explanation again.** Keep the conversation, sent screenshots, selected references and a reviewed stopping-point report together. Open the game later and check the saved situation with the AI before continuing.
- **Review decisions using the original record.** Revisit earlier exchanges and screenshots, discuss the game so far or review it after the final turn. Tactical notes help distinguish what was intended from what actually happened.
- **Carry useful lessons into later games.** The AI is instructed to record useful lessons during ordinary play, without requiring a separate review. A shared experience library lets later games and different selected models consult those lessons.

Voice input is optional. You can play entirely by typing and sending board screenshots.

[![VASL AI Bridge main window, with AI connection and game controls above the board and chat areas](screenshots/Main.png)](screenshots/Main.png)

*The main window. Click any screenshot in this guide to open the full-size image.*

## Quick Start

### Before you begin

#### Required

- A Windows 64-bit PC.
- [VASSAL](https://vassalengine.org/download.html) and the [VASL module](https://vasl.info/download.htm), with your scenario ready to open.
- The [ChatGPT desktop app for Windows](https://learn.chatgpt.com/docs/windows/windows-app), installed on the same PC. VASL AI Bridge uses its Codex component to connect to OpenAI. You do not need to operate the ChatGPT desktop app during a game.
- A ChatGPT account with access to Codex and a usable model. The application does not require a paid subscription; OpenAI controls which models and connection methods your account can use. See the Free/Go note below.
- An internet connection for sign-in and AI replies.
- Your own rules, combat charts and scenario materials. These are not supplied with VASL AI Bridge.

#### Recommended

- **ChatGPT Plus or a higher plan** for access to the Astra/Sol choices recommended below. The current Astra/Sol recommendations require Plus or higher; they are not Free/Go options. Actual access also depends on OpenAI's rollout and any workspace restrictions.
- **Optional voice input:** a microphone, a local Whisper speech engine and a separately downloaded speech model. For a straightforward setup, use **whisper.cpp for Windows x64** with the multilingual **GGML base model**. Download the engine and model manually; neither is included with VASL AI Bridge. The [voice setup steps](#optional-voice-input) are below. Typing supports the full game.

### 1. Install and sign in

Download the Windows Setup installer from [Releases](https://github.com/lingqi19850821/VASL-AI-Bridge/releases), run it, then open **VASL AI Bridge** from the Start menu.

Click **Sign in with ChatGPT** and complete the browser sign-in. Return to VASL AI Bridge; the button changes to **Sign out** when sign-in succeeds. If you closed the browser page before finishing, click **Cancel sign-in** and try again.

The browser may ask you to choose an account to continue to **Codex**. This is expected: VASL AI Bridge uses the Codex component to connect to OpenAI. Use your ChatGPT account.

**Model** and **Reasoning** stay empty and disabled until you sign in. After sign-in, the model list loads automatically; choose a **Model** and **Reasoning**. Use **Refresh models** to reload the choices if needed. These selections are not restored after signing out or restarting. Available models and usage depend on your account; replies use its Codex allowance.

**Developer's recommendation — Plus or higher:** Start with **GPT-6.1 Sol** and **Reasoning: Max**, when offered by your account. This is my recommendation for your first game and for a session lasting several hours.

| Model | What it means for your game |
| --- | --- |
| **GPT-6 Astra** | OpenAI's flagship model for the most demanding work. Choose it when you want the strongest available capability and accept faster allowance consumption. On a limited plan, a long game may reach your usage limit sooner. |
| **GPT-6.1 Sol** | OpenAI describes it as close to Astra in capability at a lower cost. It usually uses less of your allowance, making it my recommended choice for learning the application or playing a long session. |

**Astra and Sol draw on the same account's Codex allowance.** Changing from Astra to Sol does not give you a fresh allowance. Using Sol can help that allowance last longer. How long it lasts depends on the game, conversation length, references consulted and reasoning setting; neither model guarantees a certain number of playing hours.

**What Max and Ultra mean:** Max gives the model more time to reason about one task. In OpenAI's own clients, Ultra can split suitable work among several AI agents working in parallel. VASL AI Bridge does not enable that parallel-agent workflow. For the tactical decisions of one ASL opponent, **my recommendation is Max**. Higher reasoning settings can take longer and use more of your allowance; response speed also varies with the model, workload and service conditions.

**Free or Go:** Choose **GPT-6 Luna** from **Model** when it is listed, or choose another model offered to your account. The Astra/Sol recommendations above are for Plus or higher. See [OpenAI's model overview](https://learn.chatgpt.com/docs/models) and [current plan information](https://learn.chatgpt.com/docs/pricing) for availability and usage limits.

### 2. Create the game's folder

Open the scenario and starting board in VASL. In VASL AI Bridge, click **New game**.

Choose a **Location** where you want to keep the game, enter a **Game / folder name**, then click **Create game**. Choose a location you can write to, such as a folder in Documents.

This creates one folder for the conversation, references, screenshots and save points. You choose the location once; later saves stay inside that game folder.

[![New game dialog with Location and Game / folder name fields](screenshots/New%20game.png)](screenshots/New%20game.png)

### 3. Add and select the references

Click **Add files...** to import your rules, relevant combat charts, scenario sheet and any useful images. Imported files enter a reusable local library and are selected for the current game.

Open **Choose references...**, check the boxes beside the files the AI should read, then click **OK**. The library can be reused in later games.

[![Reference library showing checkboxes, a highlighted file name and the Remove from library button](screenshots/Reference.png)](screenshots/Reference.png)

**A highlighted row is not a checked box.** In this screenshot, the first row is highlighted, but no files are checked. Tick the files you want the AI to use before clicking **OK**. See [Managing reference files](#managing-reference-files) for removal instructions.

Supply all rules needed by your scenario, including extra or module rules where applicable. Rules and charts may share one document or span several files. The AI is asked to check basic rule/chart readability before play; that check does not verify that every required rule is present. Supply anything it reports missing and clarify requirements through ordinary chat.

### 4. Attach the starting board

Under **Board window**, select the VASL window containing your board. Click **Refresh windows** if it is missing.

Click **Capture and select...**. Drag around the area you want to show, then click **Use selection**, or choose **Keep full image**. Check the image attached to your draft. It stays local until you click **Send**.

[![Select screenshot area dialog with a rectangle around the VASL board and Use selection and Keep full image buttons](screenshots/screenshot.png)](screenshots/screenshot.png)

### 5. Start the conversation

Tell the AI which scenario you are playing, which side you want to control and the current turn and phase. Ask it to consult the selected materials, check the attached board and confirm the setup and victory conditions before play begins.

For example:

> Let's play the scenario in my references. I will play the Americans and you will play the Germans. Check the scenario, forces, setup and victory conditions against the attached starting board, and confirm where we should begin.

Change the sides to match your game, then click **Send**. You can use your preferred language.

During play, carry out the agreed actions in VASL and report dice, results, changed unit states and anything the AI cannot see. Send a fresh board image when needed. Check its interpretation of the board and rules, and correct misunderstandings in the same chat. **Stop** interrupts a reply when you need to intervene.

**You are ready to play.** Voice setup is optional and can wait until later.

## Save and Continue

### Autosave: Current progress

**Every game folder has one autosave, shown as Current progress. You do not need to click Save to keep the conversation you have already sent.** The application updates this local record as you play. Each update replaces the previous autosave state with the latest progress, while retaining the game's stored conversation, screenshots and references.

Each game has its own autosave. Starting another game in a different folder keeps the first game's records in its original folder. To continue a game later, open its folder and choose **Current progress**.

### Manual save points

Use **Save** when you want to keep a particular stopping point, such as the end of a turn, so you can return to it later. You can keep several named save points in the same game folder, just as a game can have one autosave and several manual saves:

1. Click **Save** to ask the AI for a game report.
2. Check the scenario, sides, objectives, turn and phase, important unit positions and states, and any unresolved actions.
3. Correct mistakes or omissions in the chat and review the updated report.
4. Click **Save point...**, which appears when the report is ready. Enter a name, or select an existing name and confirm overwriting it.
5. Save the matching board separately in VASL.

**Save** prepares the report; **Save point...** creates the named point. Several points share one game folder and the same stored originals, rather than copying every screenshot and reference into a separate complete archive each time.

Later autosaves do not overwrite your named save points. A named point stays available until you explicitly overwrite or delete it.

**If the AI becomes unavailable during play:** usage limits, a lost connection or a service interruption can prevent a new AI reply or save report. The application shows an error explaining the problem. Local **Current progress** records the conversation and screenshots you have already sent without requiring an AI reply. You can later reopen **Current progress**, then ask the AI to check and correct the stopping point once service is available again. **Save** still requests an AI report; it does not bypass an unavailable service. Check any local-save error before closing; unsent drafts and attachments are not part of the stored record. Save the matching board separately in VASL.

### Continue later

1. Open the matching board save in VASL.
2. Open VASL AI Bridge and sign in if needed.
3. Click **Open...**. Choose the game, or use **Browse...** to select its folder.
4. Under **Continue from**, choose **Current progress** or a named save point, then click **Open**.
5. Attach a fresh board screenshot and ask the AI to check the saved situation before continuing.

[![Open game dialog with a game-folder choice and Continue from set to Current progress](screenshots/Open%20game.png)](screenshots/Open%20game.png)

**Keep both records:** VASL AI Bridge preserves the conversation and game record. The VASL board save preserves the board itself. Opening one does not restore the other. Close VASL AI Bridge before moving or copying a game, and keep its whole game folder together.

**Returning to an earlier save point does not roll back shared experience.** Later chat is excluded from that point's restored game context, but lessons already added to the common experience library can remain available and influence play.

### Manage games and storage

Open **Games and storage...** to see each game's disk usage and named save points.

[![Games and storage dialog with disk usage, save-point list and cleanup controls](screenshots/Storage.png)](screenshots/Storage.png)

- **Delete selected save point...** removes that point. Shared originals needed by current progress or the game record remain, so it may free little space.
- **Clear all board screenshots...** clears the selected game's board images across **all** its save points. Chat, tactical notes, lessons and reference files remain, but the AI can no longer inspect those original board images during review. The application asks for confirmation.
- **Delete game data...** removes that game's managed records, points and materials. It preserves unrelated files and the shared experience library. The active game cannot be removed; switch to another game first.

Reference images, such as a scenario card or Order of Battle, are reference files and are kept when you clear board screenshots.

## History and Review

Scroll back through the conversation yourself, or ask the AI to find an earlier exchange and inspect its original screenshot. The stored record remains available for lookup as a game grows.

Click **Review game** to discuss the available game record up to the current stopping point. You can review an unfinished game, continue playing afterward, or review after the final turn. There is no required End Game step.

For example:

> Review the game so far. Look back at the original messages, screenshots and tactical notes. Which decisions mattered most, what actually happened, and what alternatives should we discuss?

The AI is instructed to keep concise tactical notes and record useful lessons from reported outcomes and corrections during normal play. You do not need to request a review first. Those shared lessons can be consulted in later games, including when a different model is selected.

Treat lessons as material to discuss and check against the rules and evidence. Models can make mistakes. A later explanation of an intention is not the same as an intention recorded at the time, and a screenshot that has been cleared is no longer available for inspection.

## Controls and Settings

### ChatGPT and Codex

**ChatGPT** is the desktop application you install and the account you use to sign in. **Codex** is OpenAI's agent component within that application. VASL AI Bridge uses the local Codex runtime to connect to OpenAI with your ChatGPT account; this is why the name Codex appears in connection and usage information. You play through VASL AI Bridge and do not need to use the Codex interface during a game.

### Connection and game controls

| Control | Purpose |
| --- | --- |
| **OpenAI status** | Open OpenAI's official service-status page in your browser. Located beside Settings... and available even before sign-in or while waiting for a reply. |
| **Sign in with ChatGPT / Sign out** | Connect or sign out. Signing out retains the local game and unsent draft. |
| **Cancel sign-in** | Cancel an unfinished browser sign-in so you can retry. |
| **Model / Refresh models** | Choose a model or update the available choices after signing in. |
| **Reasoning** | Choose an effort supported by the selected model for the current sign-in. |
| **New game** | Create a game folder and conversation, keeping the reusable reference library. |
| **Save / Save point...** | Prepare a reviewed report, then create or overwrite a named point. |
| **Open...** | Continue from a game's current progress or a named point. |
| **Review game** | Discuss the available game record at any stopping point. |
| **Games and storage...** | Inspect disk usage, manage points and clear game data. |

The memory status beside **Games and storage...** reports tactical notes, shared lessons or messages waiting to be recorded. Read any storage-error message before assuming those records have been saved.

If the server rejects a selected model for your account, the application asks you to choose another model or check your subscription. Usage limits, temporary request limits, expired sign-in and connection or service problems each show a corresponding message.

If replies are unusually slow or requests fail, click **OpenAI status** beside **Settings...** to check [OpenAI's service-status page](https://status.openai.com/). It reports published service incidents; individual users may experience different availability. This button opens a web page and does not send a message to AI.

### Managing reference files

**Checkboxes control AI access; highlighted rows control removal. Ticking several boxes does not select those files for removal.**

- **Check a box** to let the AI read that file in the current game. **Uncheck it** to remove it from the current selection while keeping its imported copy in the library.
- **Click a file name** to highlight its row. Hold **Ctrl** while clicking to highlight several individual rows, or **Shift** to highlight a range.
- Click **Remove from library...**. Check the names and count in the confirmation, then choose **Yes**. This action uses the highlighted rows, whether their boxes are checked or unchecked.
- Click **OK** in Reference library to apply the changes. **Cancel** in that window discards pending changes.

Removal affects the imported library copies and the current game's reference selection. **Your original files on disk are kept.** You can import them again with **Add files...**.

Supported materials include PDF, DOCX, common text files and PNG, JPEG, WebP or BMP images. Readable text can be searched; PDF pages and reference images can be inspected visually. Keep document titles, editions and page references clear, and ask the AI to identify its source when a rule matters.

### Board screenshots and messages

| Control | Purpose |
| --- | --- |
| **Board window / Refresh windows** | Select the VASL board to capture or refresh the window list. |
| **Capture and select...** | Capture the selected window and attach the full image or a chosen area to the draft. |
| **Select preview and attach...** | Select an area from the current preview and attach it to the draft. |
| **Remove** beside a draft image | Remove that attachment before sending. |
| **Send** | Send the draft and its attached screenshots. |
| **Stop** | Interrupt the current AI reply while retaining the conversation. |

Double-click **Screenshot preview**, or click **Screenshot** on a message, to inspect an image at full size. An attached image stays fixed even if the board or preview changes later. Capture is manual; images are shared when you send them.

Open **Settings... > Screenshot** to choose a keyboard shortcut or mouse side button. Shortcuts start disabled. Function keys are unavailable to avoid VASL conflicts. For a temporary display such as a line-of-sight check, keep the selected VASL window in front and use the shortcut while that display is visible.

### Reading and input preferences

[![Settings dialog showing screenshot bindings, text and message preferences, and optional local voice input](screenshots/Setting.png)](screenshots/Setting.png)

Under **Settings... > Text and messages**, adjust **Chat text size**, **Chat text weight** or **Interface text size**. Choose your preferred **Send a message** key and click **Save**. By default, Enter sends and Shift+Enter adds a new line.

The window adapts to the space available on your desktop. A smaller screen or larger Windows scaling may make the sidebar scroll. You can resize the window to suit your screen.

### Optional voice input

Voice recognition needs a separately installed compatible speech engine and model. You can skip it and use all gameplay features by typing.

For the recommended Windows CPU setup:

1. Open the [whisper.cpp v1.8.2 release](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.8.2). Under **Assets**, download **whisper-bin-x64.zip** and extract it. Keep the extracted files together.
2. Open the [Whisper model files](https://huggingface.co/ggerganov/whisper.cpp/tree/main) and manually download **ggml-base.bin**, the multilingual base model. The engine and model are separate downloads. The multilingual file has no **.en** in its name.
3. Open **Settings... > Local voice input**. Choose **whisper.cpp (CPU)**, set **Program** to the extracted **whisper-cli.exe**, set **Model** to **ggml-base.bin**, then click **Save**.
4. Click **Record voice**, speak, then click **Stop recording**. Review and edit the resulting draft before **Send**.

If you already have a compatible local installation, you can use **Automatic local engine (recommended)** or **Whisper Desktop (existing installation)** with its existing model instead. The application checks engine compatibility; a Desktop executable is not the same program as **whisper-cli.exe**.

Spoken language is detected automatically. **Cancel voice** cancels recording or transcription while preserving the existing draft and attachments. Voice configuration is independent of the first-game setup.

## Privacy

**Stored on your computer.** The reference library, game records, screenshots, tactical notes, shared experience and settings are stored locally. Game records and tactical journals are not encrypted. Notes may include the AI's tactical intentions; recreational fair play relies on players choosing whether to inspect them.

**Online requests go to OpenAI.** Sign-in and AI services connect to OpenAI through the local Codex runtime. The developer does not receive your conversations, reference files, screenshots or voice recordings through this application, and does not operate a server that collects them. Draft text and attachments remain on your computer until you send them. Sending a message, requesting an AI save report or starting a game review sends content to OpenAI; the AI can also consult selected references, earlier game records and shared lessons during that request. These materials go to OpenAI for the reply. Your use of OpenAI's services must follow its applicable [terms](https://openai.com/policies/terms-of-use/) and [Usage Policies](https://openai.com/policies/usage-policies/), including its safety and abuse-prevention rules. OpenAI handles the information it receives under its [Privacy Policy](https://openai.com/policies/privacy-policy/).

**Voice is transcribed locally.** The application uses your local speech engine and does not send the recording to OpenAI. The resulting text stays in your draft until you send it.

## Credits

Concept, requirements and product direction by [lingqi19850821](https://github.com/lingqi19850821). All original project code was written by OpenAI Codex under the developer's direction.

## License

VASL AI Bridge is licensed under the **GNU Affero General Public License v3.0**, AGPL-3.0-only. See the [project license](https://github.com/lingqi19850821/VASL-AI-Bridge/blob/main/LICENSE).

Third-party components retain their own licenses. See [Third-party notices](packaging/THIRD_PARTY_NOTICES.md).
