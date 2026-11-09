/* wallpaper-app.js -- renders the 3D project space as a live wallpaper (requirement 18).
 * Runs in the separate wallpaper window that main.js attaches behind the desktop icons.
 * Non-interactive: the scene slowly orbits, showing the user's saved project. */
import { Space3D } from "./space3d/scene.js";

const space = new Space3D(document.getElementById("wall"), { interactive: false });
await space.init();
space.setWallpaper(true);
space.autoOrbit = true;
space.open();

if (window.jarvis) window.jarvis.onWallpaperState((state) => { if (state) space.load(state); });
