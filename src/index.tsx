import {
  ButtonItem,
  Field,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import {
  addEventListener,
  callable,
  definePlugin,
  removeEventListener,
  toaster,
} from "@decky/api";
import { useEffect, useState } from "react";
import { FaGamepad } from "react-icons/fa";

type Host = { address: string; name: string };

type State = {
  running: boolean;
  state: "off" | "starting" | "waiting" | "pairing" | "connected";
  host: string;
  hosts: Host[];
  screen_off: boolean;
  options: { screen_off: boolean; deadzone: number };
  error: string | null;
};

const getState = callable<[], State>("get_state");
const setEnabled = callable<[enabled: boolean], State>("set_enabled");
const pair = callable<[], void>("pair");
const toggleScreen = callable<[], void>("toggle_screen");
const setOption = callable<[key: string, value: boolean | number], State>("set_option");

function statusText(s: State): string {
  switch (s.state) {
    case "off":
      return "Off";
    case "starting":
      return "Starting…";
    case "pairing":
      return "Ready to pair. On your Mac, open Bluetooth settings and connect to “DualSense Wireless Controller”.";
    case "waiting":
      return s.hosts.length ? `Connecting to ${s.hosts[0].name}…` : "Waiting for a device…";
    case "connected":
      return `Connected to ${s.host}`;
  }
}

function useBackendState(): [State | null, (s: State) => void] {
  const [state, setState] = useState<State | null>(null);
  useEffect(() => {
    getState().then(setState);
    const listener = addEventListener<[State]>("state", setState);
    return () => {
      removeEventListener("state", listener);
    };
  }, []);
  return [state, setState];
}

function Content() {
  const [s, setState] = useBackendState();
  const [busy, setBusy] = useState(false);
  if (!s) return null;

  const onToggle = async (enabled: boolean) => {
    setBusy(true);
    try {
      setState(await setEnabled(enabled));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ToggleField
            label="Use as controller"
            description="The Deck appears as a wireless PS5 controller."
            checked={s.running}
            disabled={busy}
            onChange={onToggle}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Status" description={statusText(s)} />
        </PanelSectionRow>
        {s.error && (
          <PanelSectionRow>
            <Field label="Last error" description={s.error} />
          </PanelSectionRow>
        )}
        {s.running && s.state !== "connected" && s.state !== "pairing" && (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => pair()}>
              Pair a new device
            </ButtonItem>
          </PanelSectionRow>
        )}
        {s.state === "connected" && (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => toggleScreen()}>
              {s.screen_off ? "Turn Deck screen on" : "Turn Deck screen off"}
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Options">
        <PanelSectionRow>
          <ToggleField
            label="Turn off screen while connected"
            checked={s.options.screen_off}
            onChange={async (v) => setState(await setOption("screen_off", v))}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label="Stick deadzone"
            value={Math.round(s.options.deadzone * 100)}
            min={0}
            max={25}
            step={1}
            showValue
            valueSuffix="%"
            onChange={async (v) => setState(await setOption("deadzone", v / 100))}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="While connected">
        <PanelSectionRow>
          <Field
            description={
              "Tap ⋯ to turn the Deck screen on or off. Hold ⋯ for 2 seconds to stop. " +
              "Bluetooth controllers and keyboards paired to the Deck are unavailable while this is on."
            }
          />
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  let lastState: State["state"] = "off";
  const stateListener = addEventListener<[State]>("state", (s) => {
    if (s.state === "connected" && lastState !== "connected") {
      toaster.toast({ title: "Deck as Controller", body: `Connected to ${s.host}` });
    } else if (lastState === "connected" && s.state !== "connected") {
      toaster.toast({ title: "Deck as Controller", body: "Disconnected" });
    }
    lastState = s.state;
  });
  const errorListener = addEventListener<[string]>("error", (message) => {
    toaster.toast({ title: "Deck as Controller", body: message });
  });

  return {
    name: "Deck as Controller",
    titleView: <div className={staticClasses.Title}>Deck as Controller</div>,
    content: <Content />,
    icon: <FaGamepad />,
    onDismount() {
      removeEventListener("state", stateListener);
      removeEventListener("error", errorListener);
    },
  };
});
