import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Bar button + popup: lists AirPlay receivers (bin/omashare scan) and runs
// one bin/omashare mirror process while sharing. The helper prints JSON
// lines ({"event": ...}); stopping is SIGTERM, which it turns into a TEARDOWN.
Panel {
  id: root
  moduleName: "lcd1232.omashare"
  ipcTarget: "lcd1232.omashare"

  readonly property string pluginDir: {
    var s = String(Qt.resolvedUrl("."))
    if (s.indexOf("file://") === 0) s = s.substring(7)
    return s
  }
  readonly property string helperPath: pluginDir + (pluginDir.endsWith("/") ? "" : "/") + "bin/omashare"

  function boolSetting(name, fallback) {
    var v = setting(name, fallback)
    return v === true || v === "true"
  }
  readonly property int fps: Number(setting("fps", 30)) || 30
  readonly property string maxResolution: String(setting("maxResolution", "1920x1080"))
  readonly property string audioMode: {
    var v = String(setting("audio", "tv"))
    return ["tv", "both", "off"].indexOf(v) >= 0 ? v : "tv"
  }
  readonly property string source: setting("source", "picker") === "monitor" ? "monitor" : "picker"
  readonly property bool showCursor: boolSetting("showCursor", true)
  readonly property bool notify: boolSetting("notify", true)
  readonly property int timingPort: {
    var v = Number(setting("timingPort", 47010))
    return v >= 1024 && v <= 65535 ? Math.floor(v) : 47010
  }

  property var devices: []
  property bool scanning: false
  property string scanError: ""

  // idle | choosing | connecting | pairing | streaming | stopping | error,
  // and for the one-time PIN pairing: pin-request | pin | pin-check
  property string phase: "idle"
  property var target: null
  property string error: ""
  property string _stderrTail: ""
  property bool _userStopped: false
  property bool _streamed: false
  property bool _paired: false
  // A share this widget did not start (a shell restart lost its process, or
  // it was started from a terminal): known only through `omashare status`.
  property bool external: false

  readonly property bool pinPairing: phase === "pin-request" || phase === "pin" || phase === "pin-check"

  // ufw drops the receiver's timing requests until the port is allowed
  // (bin/omashare firewall). phase "firewall" offers the one-time rule
  // before sharing to pendingDevice.
  property bool firewallBlocked: false
  property string firewallCommand: ""
  property var pendingDevice: null

  property int deviceIndex: 0
  property bool cursorActive: false

  readonly property bool sharing: phase === "choosing" || phase === "connecting" || phase === "pairing" || phase === "streaming"

  // The rescan button shows dots counting up 1-2-3 while a scan runs,
  // padded to three characters so the button keeps its width.
  property int dotCount: 1
  readonly property string scanDots: ".".repeat(dotCount) + "\u00a0".repeat(3 - dotCount)
  onScanningChanged: dotCount = 1

  readonly property string statusText: {
    if (phase === "streaming") return "Sharing to " + (target ? target.name : "receiver")
    if (phase === "choosing") return "Choose what to share"
    if (phase === "connecting") return "Connecting"
    if (phase === "pairing") return "Pairing"
    if (phase === "pin-request") return "Asking " + (target ? target.name : "the receiver") + " for a PIN"
    if (phase === "pin") return "Enter the PIN shown on " + (target ? target.name : "the TV")
    if (phase === "pin-check") return "Checking the PIN"
    if (phase === "stopping") return "Stopping"
    if (phase === "firewall") return "Firewall blocks receivers"
    if (phase === "error") return "Failed"
    if (scanning) return "Searching for receivers"
    return devices.length ? devices.length + (devices.length === 1 ? " receiver" : " receivers") : "Not sharing"
  }

  function scan() {
    if (scanProc.running) return
    scanning = true
    scanError = ""
    scanProc.command = [helperPath, "scan", "--timeout", "3"]
    scanProc.running = true
  }

  function applyScan(text) {
    scanning = false
    try {
      var parsed = JSON.parse(String(text || "").trim() || "[]")
      devices = Array.isArray(parsed) ? parsed : []
    } catch (e) {
      devices = []
      scanError = "Scan failed"
    }
    if (deviceIndex >= devices.length) deviceIndex = Math.max(0, devices.length - 1)
  }

  function start(device) {
    if (!device || mirrorProc.running || pairProc.running) return
    target = device
    error = ""
    _stderrTail = ""
    _userStopped = false
    _streamed = false
    phase = source === "picker" ? "choosing" : "connecting"
    var cmd = [helperPath, "mirror", device.address,
               "--id", String(device.identifier || ""),
               "--name", String(device.name || device.address),
               "--source", source,
               "--port", String(device.port || 7000),
               "--fps", String(fps),
               "--max-size", maxResolution,
               "--audio", audioMode,
               "--timing-port", String(timingPort)]
    if (!showCursor) cmd.push("--no-cursor")
    mirrorProc.command = cmd
    mirrorProc.running = true
    // Get out of the way of the screen-share dialog.
    if (source === "picker") close()
  }

  function stop() {
    if (external) {
      phase = "stopping"
      stopProc.running = true
      return
    }
    if (!mirrorProc.running) return
    _userStopped = true
    phase = "stopping"
    mirrorProc.running = false  // SIGTERM
  }

  function checkStatus() {
    if (!statusProc.running && !mirrorProc.running) statusProc.running = true
  }

  function applyStatus(text) {
    var status
    try { status = JSON.parse(String(text || "").trim()) } catch (e) { return }
    if (mirrorProc.running) return
    if (status.running) {
      external = true
      target = { name: status.name || "receiver", address: status.host, identifier: status.id, paired: true }
      if (phase !== "stopping") phase = "streaming"
    } else if (external) {
      external = false
      phase = "idle"
    }
  }

  function activate(index) {
    var device = devices[index]
    if (!device || pinPairing) return
    if (sharing && target && target.address === device.address) stop()
    else if (!mirrorProc.running) {
      if (firewallBlocked) {
        pendingDevice = device
        target = device
        error = ""
        phase = "firewall"
        checkFirewall()
      } else {
        proceed(device)
      }
    }
  }

  function proceed(device) {
    if (device.paired) start(device)
    else startPinPairing(device)
  }

  function checkFirewall() {
    if (firewallProc.running) return
    firewallProc.command = [helperPath, "firewall", "--timing-port", String(timingPort)]
    firewallProc.running = true
  }

  function applyFirewall(text) {
    var result
    try { result = JSON.parse(String(text || "").trim()) } catch (e) { return }
    firewallBlocked = result.allowed === false
    firewallCommand = String(result.command || "")
    // The rule was added while the panel waited: carry on with the share.
    if (phase === "firewall" && !firewallBlocked) continuePending()
  }

  function allowInFirewall() {
    if (firewallCommand === "") return
    // Omarchy runs privileged steps in a floating terminal; sudo asks there.
    Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation", firewallCommand])
  }

  function continuePending() {
    var device = pendingDevice
    pendingDevice = null
    phase = "idle"
    if (device) proceed(device)
  }

  function cancelFirewall() {
    pendingDevice = null
    phase = "idle"
  }

  // One-time pairing: the receiver shows a PIN, which goes to the helper on
  // stdin (never in argv), and on success the share starts right away.
  function startPinPairing(device) {
    if (pairProc.running || mirrorProc.running) return
    target = device
    error = ""
    _paired = false
    phase = "pin-request"
    pairProc.command = [helperPath, "pair", device.address,
                        "--port", String(device.port || 7000),
                        "--id", String(device.identifier || ""),
                        "--name", String(device.name || device.address)]
    pairProc.running = true
  }

  function submitPin(pin) {
    var code = String(pin || "").trim()
    if (phase !== "pin" || !/^[0-9]{4}$/.test(code)) return
    phase = "pin-check"
    pairProc.write(code + "\n")
  }

  function cancelPinPairing() {
    if (!pairProc.running) return
    pairProc.write("\n")  // an empty line cancels
    phase = "idle"
  }

  function markPaired(id, paired) {
    var next = devices.map(function(d) {
      if (String(d.identifier).toUpperCase() !== String(id).toUpperCase()) return d
      var copy = Object.assign({}, d); copy.paired = paired; return copy
    })
    devices = next
    if (target && String(target.identifier).toUpperCase() === String(id).toUpperCase()) {
      var t = Object.assign({}, target); t.paired = paired; target = t
    }
  }

  function handlePairEvent(line) {
    var msg
    try { msg = JSON.parse(line) } catch (e) { return }
    if (msg.event === "pin") {
      phase = "pin"
      pinField.text = ""
      pinField.forceActiveFocus()
    } else if (msg.event === "paired") {
      _paired = true
      markPaired(msg.id, true)
    } else if (msg.event === "error") {
      error = msg.code === "cancelled" ? "" : String(msg.message || "Pairing failed")
    }
  }

  function pairFinished(exitCode) {
    if (_paired) {
      phase = "idle"
      notifySend("Paired", (target ? target.name : "Receiver") + " is paired with this computer")
      start(target)
      return
    }
    phase = error !== "" ? "error" : "idle"
  }

  function notifySend(summary, body) {
    if (notify) Quickshell.execDetached(["notify-send", "-a", "Omashare", "-i", "video-display", summary, body || ""])
  }

  function handleEvent(line) {
    var msg
    try { msg = JSON.parse(line) } catch (e) { return }
    if (msg.event === "choosing") phase = "choosing"
    else if (msg.event === "connecting") phase = "connecting"
    else if (msg.event === "pairing") phase = "pairing"
    else if (msg.event === "streaming") {
      phase = "streaming"
      _streamed = true
      notifySend("Sharing screen", "Mirroring " + msg.monitor + " to " + (target ? target.name : "receiver")
        + (msg.audio === "off" ? " (no audio)" : ""))
    } else if (msg.event === "error") {
      error = String(msg.message || "Mirroring failed")
      if (msg.code === "not-paired" && msg.id) markPaired(msg.id, false)
    }
  }

  function finished(exitCode) {
    var wasStreaming = _streamed
    if (!_userStopped && (exitCode !== 0 || error !== "")) {
      phase = "error"
      if (error === "") error = _stderrTail || ("Helper exited with code " + exitCode)
      notifySend("Screen sharing failed", error)
      return
    }
    phase = "idle"
    if (wasStreaming) notifySend("Screen sharing stopped", target ? target.name : "")
  }

  onOpenedChanged: {
    if (!opened) return
    cursorActive = false
    checkStatus()
    checkFirewall()
    if (!sharing) scan()
  }

  Component.onCompleted: {
    checkStatus()
    checkFirewall()
    scan()
  }

  Process {
    id: firewallProc
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.applyFirewall(text) }
  }

  // Notice the rule as soon as the terminal has added it.
  Timer {
    interval: 1500
    repeat: true
    running: root.phase === "firewall"
    onTriggered: root.checkFirewall()
  }

  Process {
    id: statusProc
    command: [root.helperPath, "status"]
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.applyStatus(text) }
  }

  Process {
    id: stopProc
    command: [root.helperPath, "stop"]
    onExited: root.checkStatus()
  }

  // Follow an external share until it ends.
  Timer {
    interval: 3000
    repeat: true
    running: root.external
    onTriggered: root.checkStatus()
  }

  Timer {
    interval: 400
    repeat: true
    running: root.scanning
    onTriggered: root.dotCount = root.dotCount % 3 + 1
  }

  Process {
    id: pairProc
    stdinEnabled: true
    stdout: SplitParser { onRead: function(line) { root.handlePairEvent(line) } }
    onExited: function(exitCode) { root.pairFinished(exitCode) }
  }

  Process {
    id: scanProc
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.applyScan(text) }
    onExited: function(exitCode) {
      root.scanning = false
      if (exitCode !== 0 && root.scanError === "") root.scanError = "Scan failed (exit " + exitCode + ")"
    }
  }

  // The first scan also installs the helper's Python environment, so the
  // backstop is generous.
  Timer {
    interval: 120000
    running: scanProc.running
    onTriggered: { scanProc.running = false; root.scanError = "Scan timed out" }
  }

  Process {
    id: mirrorProc
    stdout: SplitParser { onRead: function(line) { root.handleEvent(line) } }
    stderr: SplitParser {
      onRead: function(line) {
        var l = String(line).trim()
        if (l !== "") root._stderrTail = l
      }
    }
    onExited: function(exitCode) { root.finished(exitCode) }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.sharing ? "󰄙" : "󰄘"
    // The theme's active colour, as the screen-recording indicator uses.
    active: root.sharing
    tooltipText: root.phase === "streaming" ? root.statusText : "AirPlay screen sharing"
    onPressed: function(b) {
      if (b === Qt.RightButton && root.sharing) root.stop()
      else root.toggle()
    }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (root.devices.length === 0) return
        if (!root.cursorActive) { root.cursorActive = true; return }
        var delta = dy !== 0 ? dy : dx
        root.deviceIndex = Math.max(0, Math.min(root.devices.length - 1, root.deviceIndex + delta))
      }
      onActivateRequested: {
        if (root.cursorActive) root.activate(root.deviceIndex)
        else if (root.sharing) root.stop()
      }
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        // ---------- Hero ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight)

          Text {
            id: heroIcon
            textFormat: Text.PlainText
            text: button.text
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              text: "Screen Share"
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
              width: parent.width
            }

            Text {
              textFormat: Text.PlainText
              text: root.statusText.toUpperCase()
              color: Qt.darker(root.bar.foreground, 1.4)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
              width: parent.width
            }
          }
        }

        Text {
          visible: root.phase === "error" && root.error !== ""
          width: parent.width
          textFormat: Text.PlainText
          text: root.error
          wrapMode: Text.Wrap
          maximumLineCount: 4
          elide: Text.ElideRight
          color: Color.urgent
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        // One-time PIN pairing with a receiver omashare has not paired yet.
        Column {
          visible: root.pinPairing
          width: parent.width
          spacing: Style.space(8)

          Text {
            width: parent.width
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            text: root.phase === "pin-request"
              ? "First time with " + (root.target ? root.target.name : "this TV") + ": it will show a PIN."
              : "Type the 4-digit PIN from the TV screen."
            opacity: 0.7
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Row {
            width: parent.width
            spacing: Style.space(6)

            TextField {
              id: pinField
              width: parent.width - pairButton.width - cancelButton.width - parent.spacing * 2
              foreground: root.bar.foreground
              placeholderText: "PIN"
              enabled: root.phase === "pin"
              maximumLength: 4
              inputMethodHints: Qt.ImhDigitsOnly
              validator: RegularExpressionValidator { regularExpression: /[0-9]{0,4}/ }
              onAccepted: root.submitPin(text)
              Keys.onEscapePressed: root.cancelPinPairing()
            }

            Button {
              id: pairButton
              text: root.phase === "pin-check" ? "Checking" : "Pair"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              bordered: true
              enabled: root.phase === "pin" && pinField.text.length === 4
              onClicked: root.submitPin(pinField.text)
            }

            Button {
              id: cancelButton
              text: "Cancel"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              enabled: root.phase !== "pin-check"
              onClicked: root.cancelPinPairing()
            }
          }
        }

        // One-time firewall rule: receivers send timing requests to this
        // computer, and Omarchy's ufw drops incoming traffic.
        Column {
          visible: root.phase === "firewall"
          width: parent.width
          spacing: Style.space(8)

          Text {
            width: parent.width
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            text: "While sharing, " + (root.target ? root.target.name : "the receiver")
              + " sends timing requests to UDP port " + root.timingPort
              + " on this computer, and the firewall drops them. Allow that port once:"
            opacity: 0.7
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Text {
            width: parent.width
            textFormat: Text.PlainText
            wrapMode: Text.WrapAnywhere
            text: root.firewallCommand
            color: root.bar.foreground
            font.family: "monospace"
            font.pixelSize: Style.font.bodySmall
          }

          Button {
            width: parent.width
            iconText: "󰒃"
            text: "Allow in firewall"
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            bordered: true
            onClicked: root.allowInFirewall()
          }

          Row {
            spacing: Style.space(6)

            Button {
              text: "Share anyway"
              tooltipText: "For a firewall rule added some other way"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              onClicked: root.continuePending()
            }

            Button {
              text: "Cancel"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              onClicked: root.cancelFirewall()
            }
          }
        }

        Button {
          visible: root.sharing || root.phase === "stopping"
          width: parent.width
          iconText: "󰅙"
          text: root.phase === "stopping" ? "Stopping…" : "Stop sharing"
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
          bordered: true
          active: true
          onClicked: root.stop()
        }

        PanelSeparator { foreground: root.bar.foreground }

        // ---------- Receivers ----------
        Column {
          width: parent.width
          spacing: Style.space(6)

          Item {
            width: parent.width
            implicitHeight: Math.max(header.implicitHeight, rescan.implicitHeight)

            PanelSectionHeader {
              id: header
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: "AIRPLAY RECEIVERS"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
            }

            Button {
              id: rescan
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              iconText: root.scanning ? root.scanDots : "󰑐"
              tooltipText: root.scanning ? "Searching…" : "Search again"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              onClicked: root.scan()
            }
          }

          Text {
            visible: root.devices.length === 0
            width: parent.width
            textFormat: Text.PlainText
            text: root.scanning ? "Looking for Apple TVs on this network…"
              : (root.scanError || "No AirPlay receivers found.")
            wrapMode: Text.Wrap
            opacity: 0.6
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Repeater {
            model: root.devices
            Button {
              id: deviceRow
              required property var modelData
              required property int index
              readonly property bool current: root.sharing && root.target && root.target.address === modelData.address
              width: parent.width
              leftAlign: true
              iconText: current ? "󰄙" : "󰔂"
              // Button labels do not elide, so the label is cut to fit here.
              readonly property string label: modelData.name + "  ·  "
                + (modelData.paired ? (modelData.model || modelData.address) : "not paired")
              TextMetrics {
                id: labelMetrics
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.bodySmall
                elide: Text.ElideRight
                elideWidth: deviceRow.width - Style.font.icon - Style.spacing.controlGap
                  - Style.spacing.controlPaddingX * 2 - Style.space(12)
                text: deviceRow.label
              }
              text: labelMetrics.elidedText
              // Only the row being shared explains itself; a tooltip on every
              // row covers the section header above.
              tooltipText: current ? "Stop sharing" : ""
              fontSize: Style.font.bodySmall
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              active: current
              enabled: current || (!root.sharing && !root.pinPairing && root.phase !== "firewall")
              hasCursor: root.cursorActive && root.deviceIndex === index
              onClicked: root.activate(index)
              onHovered: function(h) {
                if (h) { root.cursorActive = true; root.deviceIndex = index }
              }
            }
          }
        }
      }
    }
  }
}
