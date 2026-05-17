import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.plasma5support as P5Support

Kirigami.FormLayout {
    id: page

    property string cfg_enabledProviders
    property int cfg_refreshIntervalSeconds

    property string zaiToken: ""
    property string zaiStatus: ""

    readonly property var allProviders: [
        {
            id: "claude",
            label: "Claude",
            requirement: "Log in at claude.ai in any supported browser (chrome, chromium, brave, edge, firefox, helium). Cookies are read automatically."
        },
        {
            id: "codex",
            label: "Codex (ChatGPT)",
            requirement: "Log in at chatgpt.com in any supported browser. Cookies are read automatically."
        },
        {
            id: "copilot",
            label: "GitHub Copilot",
            requirement: "Works automatically if you're logged into github.com in any browser profile (covers Copilot Business / Enterprise).\n\nFor personal Copilot you can instead provide a fine-grained PAT at ~/.config/waybar-ai-usage/copilot.conf:\nGITHUB_TOKEN=ghp_xxxxxxxx\nCOPILOT_QUOTA=300"
        },
        {
            id: "zen",
            label: "OpenCode Zen",
            requirement: "Log in at opencode.ai in any supported browser. Cookies are read automatically."
        },
        {
            id: "zai",
            label: "Z.ai (GLM)",
            requirement: "Requires an API key (no cookie support).\n\n1. Open https://z.ai/manage-apikey/apikey-list and log in\n2. Click 'Add new Key', name it and copy the generated key\n3. Paste below and click Save."
        }
    ]

    P5Support.DataSource {
        id: shell
        engine: "executable"
        connectedSources: []
        property var pending: ({})

        onNewData: function(sourceName, data) {
            disconnectSource(sourceName);
            if (pending[sourceName]) {
                pending[sourceName](data["stdout"] || "", data["exit code"] || 0);
                delete pending[sourceName];
            }
        }

        function run(cmd, cb) {
            pending[cmd] = cb || function(){};
            connectSource(cmd);
        }
    }

    Component.onCompleted: loadZaiToken()

    function loadZaiToken() {
        shell.run("cat ~/.config/waybar-ai-usage/zai.conf 2>/dev/null", function(out) {
            var m = out.match(/ZAI_TOKEN\s*=\s*(\S+)/);
            if (m) page.zaiToken = m[1];
        });
    }

    function saveZaiToken() {
        var content = "ZAI_TOKEN=" + page.zaiToken + "\n";
        var b64 = Qt.btoa(content);
        var cmd = "sh -c 'mkdir -p ~/.config/waybar-ai-usage && echo " + b64 +
                  " | base64 -d > ~/.config/waybar-ai-usage/zai.conf && chmod 600 ~/.config/waybar-ai-usage/zai.conf'";
        shell.run(cmd, function(_out, exitCode) {
            page.zaiStatus = exitCode === 0 ? "Saved ✓" : "Save failed (exit " + exitCode + ")";
            statusFadeTimer.restart();
        });
    }

    Timer {
        id: statusFadeTimer
        interval: 3000
        onTriggered: page.zaiStatus = ""
    }

    function isEnabled(id) {
        return (cfg_enabledProviders || "").split(",").indexOf(id) !== -1;
    }

    function toggle(id, on) {
        var list = (cfg_enabledProviders || "").split(",").filter(function(s) { return s && s !== id; });
        if (on) list.push(id);
        cfg_enabledProviders = list.join(",");
    }

    Repeater {
        model: page.allProviders
        delegate: ColumnLayout {
            required property var modelData
            Kirigami.FormData.label: modelData.label + ":"
            Layout.fillWidth: true
            spacing: 2

            CheckBox {
                text: i18n("Enabled")
                checked: page.isEnabled(modelData.id)
                onToggled: page.toggle(modelData.id, checked)
            }

            Label {
                text: modelData.requirement
                wrapMode: Text.WordWrap
                textFormat: Text.PlainText
                Layout.preferredWidth: 460
                Layout.bottomMargin: 4
                color: Kirigami.Theme.disabledTextColor
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            }

            // Z.ai token input
            RowLayout {
                visible: modelData.id === "zai"
                Layout.fillWidth: true
                Layout.bottomMargin: 12

                TextField {
                    id: tokenField
                    Layout.fillWidth: true
                    Layout.preferredWidth: 320
                    echoMode: TextInput.Password
                    placeholderText: "API key from z.ai"
                    text: page.zaiToken
                    onTextChanged: page.zaiToken = text
                }

                Button {
                    text: "Save"
                    enabled: page.zaiToken.length > 0
                    onClicked: page.saveZaiToken()
                }

                Label {
                    text: page.zaiStatus
                    color: page.zaiStatus.indexOf("✓") !== -1
                        ? Kirigami.Theme.positiveTextColor
                        : Kirigami.Theme.negativeTextColor
                    visible: page.zaiStatus.length > 0
                }
            }
        }
    }

    SpinBox {
        Kirigami.FormData.label: i18n("Refresh interval (seconds):")
        from: 30
        to: 3600
        stepSize: 30
        value: cfg_refreshIntervalSeconds
        onValueChanged: cfg_refreshIntervalSeconds = value
    }
}
