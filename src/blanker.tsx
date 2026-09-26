import { addEventListener, callable, removeEventListener } from "@decky/api";
import { useEffect, useState } from "react";

// Blanks the Deck screen while connected: a full-screen black layer (OLED pixels
// off) plus minimum backlight. Writing the backlight via sysfs has no effect in
// Game Mode, so this goes through Steam instead.

type ScreenState = { state: string; screen_off: boolean };

const getState = callable<[], ScreenState>("get_state");
const toggleScreen = callable<[], void>("toggle_screen");

let savedBrightness: number | null = null;
let lastBrightness = 1;

const brightnessListener = SteamClient.System.Display.RegisterForBrightnessChanges((b) => {
  if (savedBrightness === null) lastBrightness = b.flBrightness;
});

function dim() {
  if (savedBrightness !== null) return;
  savedBrightness = lastBrightness;
  SteamClient.System.Display.SetBrightness(0);
}

function undim() {
  if (savedBrightness === null) return;
  SteamClient.System.Display.SetBrightness(savedBrightness);
  savedBrightness = null;
}

/** Call when the plugin unloads. */
export function disposeBlanker() {
  undim();
  brightnessListener.unregister();
}

export function ScreenBlanker() {
  const [blank, setBlank] = useState(false);

  useEffect(() => {
    const apply = (s: ScreenState) => setBlank(s.state === "connected" && s.screen_off);
    getState().then(apply);
    const listener = addEventListener<[ScreenState]>("state", apply);
    return () => {
      removeEventListener("state", listener);
    };
  }, []);

  useEffect(() => {
    if (blank) dim();
    else undim();
  }, [blank]);

  if (!blank) return null;
  return (
    <div
      onClick={() => toggleScreen()}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 100000,
        background: "#000",
        cursor: "none",
      }}
    />
  );
}
