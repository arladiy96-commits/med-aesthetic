
  /* v16: let Telegram open the Mini App naturally from the bottom first.
     We expand immediately, but fullscreen is requested a moment later,
     after the WebView has a real viewport. This avoids the side-opening effect. */
  (() => {
    const tg = window.Telegram?.WebApp;
    if(!tg) return;
    try{ tg.setHeaderColor?.("#07050c"); }catch(e){}
    try{ tg.setBackgroundColor?.("#07050c"); }catch(e){}
    try{ tg.setBottomBarColor?.("#07050c"); }catch(e){}
    try{ tg.disableVerticalSwipes?.(); }catch(e){}
    try{ tg.lockOrientation?.(); }catch(e){}
    try{ tg.expand?.(); }catch(e){}
    window.__medAestheticEarlyTelegramBoot = true;
    window.__medAestheticFullscreenRequested = false;
  })();
  