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

  property var devices: []
  property bool scanning: false
  property string scanError: ""

  // idle | choosing | connecting | pairing | streaming | stopping | error
  property string phase: "idle"
  property var target: null
  property string error: ""
  property string _stderrTail: ""
  property bool _userStopped: false

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
    if (phase === "stopping") return "Stopping"
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
    if (!device || mirrorProc.running) return
    target = device
    error = ""
    _stderrTail = ""
    _userStopped = false
    phase = source === "picker" ? "choosing" : "connecting"
    var cmd = [helperPath, "mirror", device.address,
               "--source", source,
               "--port", String(device.port || 7000),
               "--fps", String(fps),
               "--max-size", maxResolution,
               "--audio", audioMode]
    if (!showCursor) cmd.push("--no-cursor")
    mirrorProc.command = cmd
    mirrorProc.running = true
    // Get out of the way of the screen-share dialog.
    if (source === "picker") close()
  }

  function stop() {
    if (!mirrorProc.running) return
    _userStopped = true
    phase = "stopping"
    mirrorProc.running = false  // SIGTERM
  }

  function activate(index) {
    var device = devices[index]
    if (!device) return
    if (sharing && target && target.address === device.address) stop()
    else if (!mirrorProc.running) start(device)
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
      notifySend("Sharing screen", "Mirroring " + msg.monitor + " to " + (target ? target.name : "receiver")
        + (msg.audio === "off" ? " (no audio)" : ""))
    } else if (msg.event === "error") {
      error = String(msg.message || "Mirroring failed")
    }
  }

  function finished(exitCode) {
    var wasStreaming = phase === "streaming" || phase === "stopping"
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
    if (!sharing) scan()
  }

  Component.onCompleted: scan()

  Timer {
    interval: 400
    repeat: true
    running: root.scanning
    onTriggered: root.dotCount = root.dotCount % 3 + 1
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
              required property var modelData
              required property int index
              readonly property bool current: root.sharing && root.target && root.target.address === modelData.address
              width: parent.width
              leftAlign: true
              iconText: current ? "󰄙" : "󰔂"
              text: modelData.name + (modelData.model ? "  ·  " + modelData.model : "")
              tooltipText: current ? "Stop sharing" : modelData.address
              fontSize: Style.font.bodySmall
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              active: current
              enabled: current || !root.sharing
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
