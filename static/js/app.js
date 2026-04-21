const config = window.fridayConfig || { assistantName: "FRIDAY", userName: "Boss" };

const elements = {
    conversation: document.getElementById("conversation"),
    commandForm: document.getElementById("command-form"),
    commandInput: document.getElementById("command-input"),
    voiceButton: document.getElementById("voice-button"),
    voiceHint: document.getElementById("voice-hint"),
    speakToggle: document.getElementById("speak-toggle"),
    connectionState: document.getElementById("connection-state"),
    syncTime: document.getElementById("sync-time"),
    reactorValue: document.getElementById("reactor-value"),
    reactorFill: document.getElementById("reactor-fill"),
    protocolValue: document.getElementById("protocol-value"),
    aiModeValue: document.getElementById("ai-mode-value"),
    armorValue: document.getElementById("armor-value"),
    flightValue: document.getElementById("flight-value"),
    combatValue: document.getElementById("combat-value"),
    wakeWordValue: document.getElementById("wake-word-value"),
    notesCount: document.getElementById("notes-count"),
    remindersCount: document.getElementById("reminders-count"),
    memoriesCount: document.getElementById("memories-count"),
    notesList: document.getElementById("notes-list"),
    remindersList: document.getElementById("reminders-list"),
    memoriesList: document.getElementById("memories-list"),
};

const recognitionClass = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null;
let recognitionActive = false;
let currentVoiceLanguage = "en-US";

function appendMessage(role, text, meta) {
    const bubble = document.createElement("article");
    bubble.className = `message ${role}`;

    const metaEl = document.createElement("span");
    metaEl.className = "message-meta";
    metaEl.textContent = meta;

    const body = document.createElement("div");
    body.textContent = text;

    bubble.append(metaEl, body);
    elements.conversation.appendChild(bubble);
    elements.conversation.scrollTop = elements.conversation.scrollHeight;
}

function updateConnectionState(mode, label) {
    elements.connectionState.textContent = label;
    elements.connectionState.className = `connection-state ${mode}`;
}

function renderList(target, items, emptyLabel) {
    target.textContent = "";

    if (!items.length) {
        const li = document.createElement("li");
        li.className = "empty";
        li.textContent = emptyLabel;
        target.appendChild(li);
        return;
    }

    items.forEach((item) => {
        const li = document.createElement("li");
        li.textContent = item;
        target.appendChild(li);
    });
}

function humanizeFlag(value, trueLabel = "Online", falseLabel = "Offline") {
    return value ? trueLabel : falseLabel;
}

function syncDashboard(data) {
    const reactor = Number(data?.status?.reactor || 0);
    currentVoiceLanguage = data?.voice?.language || "en-US";

    elements.reactorValue.textContent = `${reactor}%`;
    elements.reactorFill.style.width = `${Math.max(0, Math.min(100, reactor))}%`;
    elements.protocolValue.textContent = data?.status?.protocol || "--";
    elements.aiModeValue.textContent = data?.status?.ai_mode || "--";
    elements.armorValue.textContent = humanizeFlag(
        data?.status?.armor_deployed,
        "Deployed",
        "Stowed"
    );
    elements.flightValue.textContent = humanizeFlag(
        data?.status?.flight_mode,
        "Online",
        "Offline"
    );
    elements.combatValue.textContent = humanizeFlag(
        data?.status?.combat_mode,
        "Armed",
        "Safe"
    );
    elements.wakeWordValue.textContent = data?.voice?.wake_word || "--";
    elements.notesCount.textContent = String(data?.counts?.notes ?? 0);
    elements.remindersCount.textContent = String(data?.counts?.reminders ?? 0);
    elements.memoriesCount.textContent = String(data?.counts?.memories ?? 0);
    elements.syncTime.textContent = data?.generated_at || "Updated";

    renderList(elements.notesList, data?.notes || [], "No notes yet.");
    renderList(elements.remindersList, data?.reminders || [], "No reminders yet.");
    renderList(elements.memoriesList, data?.memories || [], "No memories yet.");
}

async function fetchDashboard() {
    try {
        const response = await fetch("/api/dashboard");
        if (!response.ok) {
            throw new Error(`Dashboard request failed with ${response.status}`);
        }
        const data = await response.json();
        syncDashboard(data);
        updateConnectionState("online", "Linked");
    } catch (error) {
        updateConnectionState("offline", "Offline");
        elements.voiceHint.textContent = "Dashboard refresh failed. The server may be restarting.";
    }
}

function speakReply(text) {
    if (!elements.speakToggle.checked || !("speechSynthesis" in window)) {
        return;
    }

    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = currentVoiceLanguage;
    utterance.rate = 1;
    window.speechSynthesis.speak(utterance);
}

async function sendCommand(command, appendUser = true) {
    const trimmed = command.trim();
    if (!trimmed) {
        return;
    }

    if (appendUser) {
        appendMessage("user", trimmed, config.userName);
    }

    elements.commandInput.value = "";
    updateConnectionState("online", "Processing");

    try {
        const response = await fetch("/ask", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: trimmed }),
        });

        if (!response.ok) {
            throw new Error(`Command request failed with ${response.status}`);
        }

        const payload = await response.json();
        const reply = payload.reply || "No response received.";
        appendMessage("assistant", reply, config.assistantName);
        speakReply(reply);
        updateConnectionState("online", "Linked");
        await fetchDashboard();
    } catch (error) {
        updateConnectionState("offline", "Offline");
        appendMessage(
            "assistant",
            "The command deck lost contact with the server. Please try again.",
            config.assistantName
        );
    }
}

function initialiseRecognition() {
    if (!recognitionClass) {
        elements.voiceButton.disabled = true;
        elements.voiceHint.textContent = "Browser voice input is not supported here.";
        return;
    }

    recognition = new recognitionClass();
    recognition.lang = currentVoiceLanguage;
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;

    recognition.addEventListener("start", () => {
        recognitionActive = true;
        elements.voiceButton.classList.add("active");
        elements.voiceHint.textContent = "Listening for a command...";
    });

    recognition.addEventListener("end", () => {
        recognitionActive = false;
        elements.voiceButton.classList.remove("active");
        elements.voiceHint.textContent = "Mic ready. Tap to send a spoken command.";
    });

    recognition.addEventListener("result", (event) => {
        const transcript = Array.from(event.results)
            .map((result) => result[0]?.transcript || "")
            .join(" ")
            .trim();

        if (!transcript) {
            return;
        }

        elements.commandInput.value = transcript;
        sendCommand(transcript);
    });

    recognition.addEventListener("error", (event) => {
        elements.voiceHint.textContent = `Voice input error: ${event.error}`;
    });

    elements.voiceHint.textContent = "Mic ready. Tap to send a spoken command.";
}

function toggleRecognition() {
    if (!recognition) {
        return;
    }

    recognition.lang = currentVoiceLanguage;

    if (recognitionActive) {
        recognition.stop();
    } else {
        recognition.start();
    }
}

function bindUI() {
    elements.commandForm.addEventListener("submit", (event) => {
        event.preventDefault();
        sendCommand(elements.commandInput.value);
    });

    elements.voiceButton.addEventListener("click", () => {
        toggleRecognition();
    });

    document.querySelectorAll("[data-command]").forEach((button) => {
        button.addEventListener("click", () => {
            sendCommand(button.dataset.command || "");
        });
    });

    document.querySelectorAll("[data-jump]").forEach((button) => {
        button.addEventListener("click", () => {
            const target = document.querySelector(button.dataset.jump || "");
            if (target) {
                target.scrollIntoView({ behavior: "smooth", block: "start" });
            }
        });
    });
}

function boot() {
    appendMessage(
        "assistant",
        `${config.assistantName} command deck online. You can type or use the mic to send commands from the browser.`,
        config.assistantName
    );
    bindUI();
    initialiseRecognition();
    fetchDashboard();
    window.setInterval(fetchDashboard, 15000);
}

boot();
