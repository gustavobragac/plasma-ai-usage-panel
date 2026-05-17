import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.extras as PlasmaExtras
import org.kde.plasma.plasma5support as P5Support
import org.kde.kirigami as Kirigami

PlasmoidItem {
    id: root

    readonly property var allProviders: [
        { id: "claude",   label: "Claude",   command: "claude-usage --waybar",   color: "#DE7356" },
        { id: "codex",    label: "Codex",    command: "codex-usage --waybar",    color: "#74AA9C" },
        { id: "copilot",  label: "Copilot",  command: "copilot-usage --waybar",  color: "#8b5cf6" },
        { id: "zen",      label: "OpenCode Zen", command: "zen-balance --waybar", color: "#DE7356" },
        { id: "zai",      label: "Z.ai",     command: "zai-usage --waybar",      color: "#126EF4" }
    ]

    readonly property var providers: {
        var enabled = (Plasmoid.configuration.enabledProviders || "claude,codex").split(",");
        var out = [];
        for (var i = 0; i < allProviders.length; i++) {
            if (enabled.indexOf(allProviders[i].id) !== -1) out.push(allProviders[i]);
        }
        return out;
    }

    property var providerData: ({})
    property string popupProvider: "claude"

    preferredRepresentation: compactRepresentation

    P5Support.DataSource {
        id: executable
        engine: "executable"
        connectedSources: []

        property var callbacks: ({})

        onNewData: function(sourceName, data) {
            var stdout = data["stdout"] || "";
            disconnectSource(sourceName);
            if (callbacks[sourceName]) {
                callbacks[sourceName](stdout);
                delete callbacks[sourceName];
            }
        }

        function exec(cmd, cb) {
            callbacks[cmd] = cb;
            connectSource(cmd);
        }
    }

    Timer {
        interval: Math.max(30, Plasmoid.configuration.refreshIntervalSeconds || 120) * 1000
        repeat: true
        running: true
        triggeredOnStart: true
        onTriggered: refreshAll()
    }

    function refreshAll() {
        for (var i = 0; i < providers.length; i++) {
            refreshProvider(providers[i]);
        }
    }

    function refreshProvider(p) {
        executable.exec(p.command, function(stdout) {
            var parsed = null;
            try { parsed = JSON.parse(stdout); }
            catch (e) { parsed = { error: true, raw: stdout }; }
            var copy = Object.assign({}, root.providerData);
            copy[p.id] = parsed;
            root.providerData = copy;
        });
    }

    function stripPango(s) {
        if (!s) return "";
        return String(s).replace(/<[^>]+>/g, "");
    }

    compactRepresentation: Item {
        implicitWidth: row.implicitWidth + 8
        implicitHeight: Math.max(row.implicitHeight + 4, 20)
        Layout.preferredWidth: implicitWidth
        Layout.preferredHeight: implicitHeight
        Layout.minimumWidth: implicitWidth
        Layout.minimumHeight: implicitHeight

        RowLayout {
            id: row
            anchors.centerIn: parent
            spacing: 16

            Repeater {
                model: root.providers

                delegate: MouseArea {
                    id: ma
                    required property var modelData
                    Layout.preferredHeight: tokenRow.implicitHeight
                    Layout.preferredWidth: tokenRow.implicitWidth
                    Layout.alignment: Qt.AlignVCenter
                    cursorShape: Qt.PointingHandCursor
                    hoverEnabled: true
                    onClicked: {
                        root.popupProvider = modelData.id;
                        root.expanded = !root.expanded;
                    }

                    RowLayout {
                        id: tokenRow
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 5

                        Repeater {
                            model: {
                                var d = root.providerData[modelData.id];
                                if (!d) return [modelData.label, "…"];
                                if (d.error) return [modelData.label, "✗"];
                                var clean = root.stripPango(d.text || modelData.label).trim();
                                return clean.split(/\s+/).filter(function(t){ return t.length > 0; });
                            }
                            delegate: PlasmaComponents.Label {
                                required property var modelData
                                required property int index
                                text: modelData
                                verticalAlignment: Text.AlignVCenter
                                // Tokens 0 and 2 are icons (provider + clock) — colorize them
                                color: (index === 0 || index === 2)
                                    ? ma.modelData.color
                                    : Kirigami.Theme.textColor
                            }
                        }
                    }
                }
            }
        }
    }

    fullRepresentation: Item {
        implicitWidth: popupColumn.implicitWidth + 24
        implicitHeight: popupColumn.implicitHeight + 24
        Layout.preferredWidth: implicitWidth
        Layout.preferredHeight: implicitHeight
        Layout.minimumWidth: implicitWidth
        Layout.minimumHeight: implicitHeight
        Layout.maximumWidth: implicitWidth + 40
        Layout.maximumHeight: implicitHeight + 40

        ColumnLayout {
            id: popupColumn
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.margins: 12
            spacing: 10

            PlasmaExtras.Heading {
                level: 2
                color: {
                    for (var i = 0; i < root.allProviders.length; i++) {
                        if (root.allProviders[i].id === root.popupProvider) return root.allProviders[i].color;
                    }
                    return Kirigami.Theme.textColor;
                }
                text: {
                    for (var i = 0; i < root.allProviders.length; i++) {
                        if (root.allProviders[i].id === root.popupProvider) return root.allProviders[i].label;
                    }
                    return root.popupProvider;
                }
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: "#444" }

            // Error state: short message pointing to Configure
            ColumnLayout {
                visible: {
                    var d = root.providerData[root.popupProvider];
                    return !d || d.error || d.class === "critical";
                }
                Layout.fillWidth: true
                spacing: 6

                PlasmaComponents.Label {
                    text: {
                        var d = root.providerData[root.popupProvider];
                        if (!d) return "Loading…";
                        return "Could not fetch usage data.";
                    }
                    color: Kirigami.Theme.negativeTextColor
                    font.bold: true
                }

                PlasmaComponents.Label {
                    text: "Right-click the widget → Configure Plasma AI Usage to set up this provider."
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                    color: Kirigami.Theme.disabledTextColor
                }
            }

            // Normal state: show tooltip as-is in monospace (preserves alignment from script)
            PlasmaComponents.Label {
                visible: {
                    var d = root.providerData[root.popupProvider];
                    return d && !d.error && d.class !== "critical";
                }
                Layout.fillWidth: true
                textFormat: Text.PlainText
                font.family: "monospace"
                font.pixelSize: Kirigami.Theme.defaultFont.pixelSize + 2
                text: {
                    var d = root.providerData[root.popupProvider];
                    if (!d || !d.tooltip) return "";
                    var lines = root.stripPango(d.tooltip).split("\n");
                    var out = [];
                    for (var i = 0; i < lines.length; i++) {
                        var l = lines[i];
                        if (l.trim().toLowerCase().indexOf("click") === 0) continue;
                        if (l.match(/^[━─=]+$/)) continue;
                        out.push(l);
                    }
                    return out.join("\n").trim();
                }
            }

            Item { Layout.fillHeight: true }

            PlasmaComponents.Button {
                text: "Refresh"
                Layout.alignment: Qt.AlignRight
                onClicked: {
                    for (var i = 0; i < root.providers.length; i++) {
                        if (root.providers[i].id === root.popupProvider) {
                            root.refreshProvider(root.providers[i]);
                            return;
                        }
                    }
                }
            }
        }
    }
}
