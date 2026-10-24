import QtQuick
import QtQuick.Controls
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
  readonly property int fps: {
    var v = Math.round(Number(setting("fps", 30)))
    return v >= 10 && v <= 60 ? v : 30
  }
  // Frame rates offered as one click; anything else is "Custom".
  readonly property var fpsPresets: [24, 30, 60]
  readonly property string maxResolution: String(setting("maxResolution", "1920x1080"))
  readonly property string audioMode: {
    var v = String(setting("audio", "tv"))
    return ["tv", "both", "off"].indexOf(v) >= 0 ? v : "tv"
  }
  readonly property string source: setting("shareSource", "picker") === "monitor" ? "monitor" : "picker"
  readonly property bool showCursor: boolSetting("showCursor", true)
  readonly property bool dndWhileSharing: boolSetting("dndWhileSharing", true)
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

  // ufw drops the receivers' timing requests until the port is allowed
  // (bin/omashare firewall); the panel then shows the command to allow it.
  property bool firewallBlocked: false
  property string firewallCommand: ""
  property bool commandCopied: false

  // Do Not Disturb while sharing. Turned off again only if this panel turned
  // it on; a DND the user had set stays. Ownership is also kept in a file,
  // so a shell restart mid-share still turns it off afterwards.
  property bool _dndOwned: false
  // What the last finished setDnd call left DND at, as far as we know.
  property bool _dndApplied: false
  property bool _dndSending: false
  // Notifications held until DND is off again, so they aren't silenced.
  property var _afterDnd: []
  readonly property string dndMarker: {
    var dir = Quickshell.env("XDG_RUNTIME_DIR")
    return dir ? dir + "/omashare-dnd-owned" : ""
  }
  property bool _pairCancelled: false
  // The receiver refused the last PIN and asked for it again.
  property bool _pinRetry: false

  // The settings view replaces the receiver list while open.
  property bool settingsOpen: false
  // Custom picked while the frame rate is still a preset: show the field.
  property bool fpsCustomOpen: false
  readonly property bool fpsCustom: fpsCustomOpen || fpsPresets.indexOf(fps) < 0

  // Save one setting into this widget's shell.json entry, as the first-party
  // panels do; the shell then hands the new values back through `settings`.
  // The bar reads some entry keys itself, so settings must not use them:
  // `source`, `type` and `exec` turn the entry into a custom module.
  function saveSetting(key, value) {
    if (!bar || !bar.shell || typeof bar.shell.updateEntryInline !== "function") return
    var entry = { id: moduleName }
    for (var k in settings) if (k !== "id") entry[k] = settings[k]
    entry[key] = value
    bar.shell.updateEntryInline(moduleName, entry)
  }
  // Text waiting to be written to wl-copy's stdin (see copyProc).
  property string _pendingCopy: ""

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
    if (phase === "error") return "Failed"
    if (settingsOpen) return "Settings"
    if (scanning) return "Searching for receivers"
    return devices.length ? devices.length + (devices.length === 1 ? " receiver" : " receivers") : "Not sharing"
  }

  // Nerd Font (Material Design) glyphs by the helper's device kind.
  function kindIcon(kind) {
    if (kind === "tv") return "󰟴"       // md-television_classic
    if (kind === "laptop") return "󰌢"   // md-laptop
    if (kind === "imac") return "󰇄"     // md-desktop_mac
    if (kind === "desktop") return "󰇅"  // md-desktop_tower
    return "󰍹"                          // md-monitor
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
    var out = String(text || "").trim()
    // A failed scan prints nothing (onExited reports it): keep the list.
    if (out === "") return
    try {
      var parsed = JSON.parse(out)
      devices = Array.isArray(parsed) ? parsed : []
    } catch (e) {
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
    } else {
      if (external) {
        external = false
        phase = "idle"
      }
      // A share this panel no longer runs (it ended, or the shell restarted).
      if (_dndOwned) releaseDnd(null)
    }
  }

  function activate(index) {
    var device = devices[index]
    if (!device || pinPairing) return
    if (sharing && target && target.address === device.address) { stop(); return }
    // The other rows are disabled while a share runs or stops.
    if (sharing || external || phase === "stopping" || mirrorProc.running) return
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
  }

  function copyFirewallCommand() {
    if (firewallCommand === "" || copyProc.running) return
    _pendingCopy = firewallCommand
    copyProc.stdinEnabled = true
    copyProc.running = true
  }

  // One-time pairing: the receiver shows a PIN, which goes to the helper on
  // stdin (never in argv), and on success the share starts right away.
  function startPinPairing(device) {
    if (pairProc.running || mirrorProc.running) return
    target = device
    error = ""
    _paired = false
    _pairCancelled = false
    _pinRetry = false
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
    restoreKeyFocus()
  }

  function cancelPinPairing() {
    if (!pairProc.running) return
    _pairCancelled = true
    // Before the PIN is asked for, the helper isn't reading yet: stop it.
    if (phase === "pin-request") pairProc.running = false
    else pairProc.write("\n")  // an empty line cancels
    phase = "idle"
    restoreKeyFocus()
  }

  // The PIN field had focus; give it back to the list's key handling.
  function restoreKeyFocus() {
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
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
    if (_pairCancelled) return
    var msg
    try { msg = JSON.parse(line) } catch (e) { return }
    if (msg.event === "pin") {
      _pinRetry = msg.retry === true
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

  function pairFinished() {
    restoreKeyFocus()
    if (_pairCancelled) {
      _pairCancelled = false
      phase = "idle"
      return
    }
    if (_paired) {
      phase = "idle"
      notifySend("Paired", (target ? target.name : "Receiver") + " is paired with this computer")
      start(target)
      return
    }
    phase = error !== "" ? "error" : "idle"
  }

  function notifySend(summary, body) {
    Quickshell.execDetached(["notify-send", "-a", "Omashare", "-i", "video-display", summary, body || ""])
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
      // After the notification above has been shown, or DND would hold it.
      if (dndWhileSharing) dndOnTimer.restart()
    } else if (msg.event === "error") {
      error = String(msg.message || "Mirroring failed")
      if (msg.code === "not-paired" && msg.id) markPaired(msg.id, false)
    }
  }

  function finished(exitCode) {
    var wasStreaming = _streamed
    dndOnTimer.stop()
    if (!_userStopped && (exitCode !== 0 || error !== "")) {
      phase = "error"
      if (error === "") error = _stderrTail || ("Helper exited with code " + exitCode)
      var failure = error
      releaseDnd(function() { notifySend("Screen sharing failed", failure) })
      return
    }
    phase = "idle"
    var name = target ? target.name : ""
    releaseDnd(function() { if (wasStreaming) notifySend("Screen sharing stopped", name) })
  }

  // Turn DND on for the share, unless it is on already.
  function claimDnd() {
    if (_dndOwned || dndStateProc.running || !mirrorProc.running) return
    dndStateProc.running = true
  }

  function applyDndState(text) {
    if (String(text || "").trim() !== "off" || !mirrorProc.running) return
    _dndOwned = true
    if (dndMarker !== "") Quickshell.execDetached(["touch", dndMarker])
    syncDnd()
  }

  // Turn DND off if the share turned it on, then run *then*.
  function releaseDnd(then) {
    if (then) _afterDnd = _afterDnd.concat([then])
    if (_dndOwned) {
      _dndOwned = false
      if (dndMarker !== "") Quickshell.execDetached(["rm", "-f", dndMarker])
    }
    syncDnd()
  }

  // Bring DND to what ownership asks for, one setDnd call at a time: a share
  // that ends while the "on" call runs gets its "off" right after it.
  function syncDnd() {
    if (dndSetProc.running) return  // onExited calls back
    if (_dndApplied !== _dndOwned) {
      _dndSending = _dndOwned
      dndSetProc.command = ["omarchy-shell", "notifications", "setDnd", _dndOwned ? "on" : "off"]
      dndSetProc.running = true
      return
    }
    var pending = _afterDnd
    _afterDnd = []
    for (var i = 0; i < pending.length; i++) pending[i]()
  }

  // A marker left by a share before a shell restart: DND is ours to undo.
  function adoptDnd() {
    _dndOwned = true
    _dndApplied = true
    checkStatus()  // turns it off unless that share still runs
  }

  onOpenedChanged: {
    if (!opened) return
    cursorActive = false
    settingsOpen = false
    fpsCustomOpen = false
    checkStatus()
    checkFirewall()
    if (!sharing) scan()
  }

  Component.onCompleted: {
    checkStatus()
    checkFirewall()
    scan()
    if (dndMarker !== "") dndMarkerProc.running = true
  }

  Process {
    id: firewallProc
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.applyFirewall(text) }
  }

  // The notice goes away once the rule is added, with the panel still open.
  Timer {
    interval: 2000
    repeat: true
    running: root.opened && root.firewallBlocked
    onTriggered: root.checkFirewall()
  }

  Timer {
    id: copiedTimer
    interval: 2000
    onTriggered: root.commandCopied = false
  }

  Process {
    id: copyProc
    command: ["wl-copy"]
    stdinEnabled: false
    onStarted: {
      write(root._pendingCopy)
      root._pendingCopy = ""
      stdinEnabled = false  // closes stdin: wl-copy takes the text
    }
    onExited: function(exitCode) {
      root._pendingCopy = ""
      stdinEnabled = false
      if (exitCode === 0) {
        root.commandCopied = true
        copiedTimer.restart()
      }
    }
  }

  Timer {
    id: dndOnTimer
    interval: 1500
    onTriggered: root.claimDnd()
  }

  Process {
    id: dndStateProc
    command: ["omarchy-shell", "notifications", "dndState"]
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.applyDndState(text) }
  }

  Process {
    id: dndSetProc
    onExited: {
      root._dndApplied = root._dndSending
      // The bar's DND indicator reads its state on refresh.
      Quickshell.execDetached(["omarchy-shell", "-q", "omarchy.indicators", "refresh"])
      root.syncDnd()
    }
  }

  Process {
    id: dndMarkerProc
    command: ["test", "-e", root.dndMarker]
    onExited: function(exitCode) { if (exitCode === 0) root.adoptDnd() }
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
    onExited: root.pairFinished()
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
        if (root.settingsOpen || root.devices.length === 0) return
        if (!root.cursorActive) { root.cursorActive = true; return }
        var delta = dy !== 0 ? dy : dx
        root.deviceIndex = Math.max(0, Math.min(root.devices.length - 1, root.deviceIndex + delta))
      }
      onActivateRequested: {
        if (root.settingsOpen) return
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

          PanelActionButton {
            id: settingsButton
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            iconText: root.settingsOpen ? "󰅖" : "󰒓"
            tooltipText: root.settingsOpen ? "Back to receivers" : "Settings"
            foreground: Qt.darker(root.bar.foreground, 1.4)
            hoverColor: root.bar.foreground
            fontFamily: root.bar.fontFamily
            onClicked: root.settingsOpen = !root.settingsOpen
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: settingsButton.left
            anchors.rightMargin: Style.space(8)
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
          visible: !root.settingsOpen && root.phase === "error" && root.error !== ""
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
          visible: !root.settingsOpen && root.pinPairing
          width: parent.width
          spacing: Style.space(8)

          Text {
            width: parent.width
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            text: root.phase === "pin-request"
              ? "First time with " + (root.target ? root.target.name : "this TV") + ": it will show a PIN."
              : (root._pinRetry
                ? "That PIN didn't match. Type the PIN from the TV screen again."
                : "Type the 4-digit PIN from the TV screen.")
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

        // Receivers send timing requests to this computer while sharing, and
        // Omarchy's ufw drops incoming traffic: shown until the rule exists.
        Column {
          visible: !root.settingsOpen && root.firewallBlocked && root.firewallCommand !== ""
          width: parent.width
          spacing: Style.space(8)

          Text {
            width: parent.width
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            text: "Firewall blocks AirPlay. Run once in a terminal:"
            color: Color.urgent
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          // The command as a code block with a copy button, like a snippet on
          // GitHub.
          Rectangle {
            width: parent.width
            implicitHeight: Math.max(commandText.implicitHeight, copyButton.height) + Style.space(8) * 2
            radius: Style.cornerRadius
            color: Style.normalFillFor(root.bar.foreground, root.bar.foreground, Color.urgent)
            border.width: Style.normalBorderWidth
            border.color: Style.normalBorderFor(root.bar.foreground, root.bar.foreground, Color.urgent)

            // Selectable, so it can be copied by hand without wl-clipboard.
            TextEdit {
              id: commandText
              anchors.left: parent.left
              anchors.leftMargin: Style.space(10)
              anchors.verticalCenter: parent.verticalCenter
              width: parent.width - Style.space(10) * 2 - copyButton.width - Style.space(6)
              readOnly: true
              selectByMouse: true
              activeFocusOnPress: false
              textFormat: TextEdit.PlainText
              wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
              text: root.firewallCommand
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            PanelActionButton {
              id: copyButton
              anchors.right: parent.right
              anchors.rightMargin: Style.space(4)
              anchors.verticalCenter: parent.verticalCenter
              iconText: root.commandCopied ? "󰄬" : "󰆏"
              tooltipText: root.commandCopied ? "Copied" : "Copy"
              foreground: root.commandCopied ? root.bar.foreground : Qt.darker(root.bar.foreground, 1.4)
              hoverColor: root.bar.foreground
              fontFamily: root.bar.fontFamily
              fontSize: Style.font.iconSmall
              onClicked: root.copyFirewallCommand()
            }
          }
        }

        Button {
          visible: !root.settingsOpen && (root.sharing || root.phase === "stopping")
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

        // ---------- Settings ----------
        Column {
          visible: root.settingsOpen
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader {
            text: "VIDEO"
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
          }

          Text {
            text: "Frame rate"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.body
          }
          ButtonGroup {
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            options: root.fpsPresets.map(function(f) { return { value: String(f), label: f + " fps" } })
              .concat([{ value: "custom", label: "Custom" }])
            value: root.fpsCustom ? "custom" : String(root.fps)
            onChanged: function(v) {
              if (v === "custom") { root.fpsCustomOpen = true; return }
              root.fpsCustomOpen = false
              root.saveSetting("fps", Number(v))
            }
          }
          NumberField {
            visible: root.fpsCustom
            label: "Frames per second (10–60)"
            from: 10
            to: 60
            value: root.fps
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            onModified: function(v) { root.saveSetting("fps", v) }
          }

          Text {
            text: "Maximum resolution"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.body
          }
          ButtonGroup {
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            options: [
              { value: "1280x720", label: "720p" },
              { value: "1920x1080", label: "1080p" },
              { value: "3840x2160", label: "4K" }
            ]
            value: root.maxResolution
            onChanged: function(v) { root.saveSetting("maxResolution", v) }
          }

          Text {
            text: "What to share"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.body
          }
          ButtonGroup {
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            options: [
              { value: "picker", label: "Ask each time" },
              { value: "monitor", label: "Focused screen" }
            ]
            value: root.source
            onChanged: function(v) { root.saveSetting("shareSource", v) }
          }

          Toggle {
            width: parent.width
            label: "Show the mouse cursor"
            checked: root.showCursor
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            titleSize: Style.font.body
            onClicked: root.saveSetting("showCursor", !root.showCursor)
          }

          PanelSectionHeader {
            text: "AUDIO"
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
          }
          ButtonGroup {
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            options: [
              { value: "tv", label: "Receiver only" },
              { value: "both", label: "Both" },
              { value: "off", label: "Off" }
            ]
            value: root.audioMode
            onChanged: function(v) { root.saveSetting("audio", v) }
          }

          PanelSectionHeader {
            text: "WHILE SHARING"
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
          }
          Toggle {
            width: parent.width
            label: "Do Not Disturb"
            description: "Keep notifications off the receiver"
            checked: root.dndWhileSharing
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            titleSize: Style.font.body
            onClicked: root.saveSetting("dndWhileSharing", !root.dndWhileSharing)
          }

          Text {
            width: parent.width
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            text: "Changes apply to the next share."
            opacity: 0.6
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
        }

        // ---------- Receivers ----------
        Column {
          visible: !root.settingsOpen
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

          // Scrolls past a few receivers instead of growing off the screen;
          // a ListView keeps the keyboard cursor's row in view, as in the
          // Bluetooth panel.
          ListView {
            id: deviceList
            width: parent.width
            height: Math.min(contentHeight, Style.space(400))
            spacing: Style.space(6)
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            interactive: contentHeight > height
            // Always drawn while the list overflows: an auto-hiding bar gives
            // no hint that more receivers are below.
            ScrollBar.vertical: ScrollBar {
              policy: deviceList.interactive ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
              contentItem: Rectangle {
                implicitWidth: Style.space(4)
                radius: width / 2
                color: Qt.darker(root.bar.foreground, 1.8)
              }
            }

            // Fades on the edges that have more rows past them.
            Rectangle {
              visible: deviceList.interactive && !deviceList.atYEnd
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.bottom: parent.bottom
              height: Style.space(28)
              gradient: Gradient {
                GradientStop { position: 0.0; color: "transparent" }
                GradientStop { position: 1.0; color: Color.popups.background }
              }
            }
            Rectangle {
              visible: deviceList.interactive && !deviceList.atYBeginning
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              height: Style.space(28)
              gradient: Gradient {
                GradientStop { position: 0.0; color: Color.popups.background }
                GradientStop { position: 1.0; color: "transparent" }
              }
            }

            model: root.devices
            currentIndex: root.cursorActive ? root.deviceIndex : -1
            // Deferred: the model is replaced on every scan.
            onCurrentIndexChanged: if (currentIndex >= 0) Qt.callLater(keepCurrentVisible)
            function keepCurrentVisible() {
              if (currentIndex >= 0) positionViewAtIndex(currentIndex, ListView.Contain)
            }

            // Two lines, like the Bluetooth panel's device rows: the name, and
            // the model or state below it in a smaller, dimmer line.
            delegate: CursorSurface {
              id: deviceRow
              required property var modelData
              required property int index
              readonly property bool usable: current || (!root.sharing && !root.pinPairing)
              readonly property string detail: current ? "Sharing"
                : (modelData.paired ? (modelData.model || modelData.address) : "Not paired")
              width: ListView.view.width
              implicitHeight: rowContent.implicitHeight + Style.spacing.rowPaddingX
              opacity: usable ? 1.0 : 0.4
              hasCursor: usable && root.cursorActive && root.deviceIndex === index
              current: !!(root.sharing && root.target && root.target.address === modelData.address)
              foreground: root.bar.foreground

              MouseArea {
                id: rowMouse
                anchors.fill: parent
                hoverEnabled: true
                enabled: deviceRow.usable
                cursorShape: Qt.PointingHandCursor
                onContainsMouseChanged: if (containsMouse) { root.cursorActive = true; root.deviceIndex = deviceRow.index }
                onClicked: root.activate(deviceRow.index)
              }

              // Only the row being shared explains itself; a tooltip on every
              // row covers the section header above.
              PanelToolTip {
                visible: deviceRow.current && rowMouse.containsMouse
                text: "Stop sharing"
                fontFamily: root.bar.fontFamily
              }

              Item {
                id: rowContent
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Style.space(10)
                anchors.rightMargin: Style.space(10)
                implicitHeight: Math.max(rowIcon.implicitHeight, rowInfo.implicitHeight)

                Text {
                  id: rowIcon
                  textFormat: Text.PlainText
                  text: deviceRow.current ? "󰄙" : root.kindIcon(deviceRow.modelData.kind)
                  color: root.bar.foreground
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.heading
                  // Glyphs differ in width; a fixed slot keeps the names aligned.
                  width: Style.font.heading * 1.3
                  horizontalAlignment: Text.AlignHCenter
                  anchors.left: parent.left
                  anchors.verticalCenter: parent.verticalCenter
                }

                Column {
                  id: rowInfo
                  spacing: Style.space(1)
                  anchors.left: rowIcon.right
                  anchors.leftMargin: Style.space(10)
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter

                  Text {
                    textFormat: Text.PlainText
                    text: deviceRow.modelData.name
                    color: root.bar.foreground
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.body
                    elide: Text.ElideRight
                    width: parent.width
                  }
                  Text {
                    textFormat: Text.PlainText
                    text: deviceRow.detail
                    color: Qt.darker(root.bar.foreground, 1.5)
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                    width: parent.width
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
