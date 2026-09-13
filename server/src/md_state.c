/* The one place that looks inside DOOM.
 *
 * This exists as its own translation unit for a mundane reason: <windows.h>
 * typedefs `boolean` as unsigned char and DOOM's doomtype.h typedefs it as an
 * enum, so the socket code and the game headers cannot share a file. Keeping
 * them apart is cheaper than patching either one.
 *
 * What it exports is a single byte per frame: enough for the service's input
 * profile to know whether the player is in a menu, playing, or dead, without
 * the service knowing anything about DOOM.
 */

#include "doomstat.h"

#define MD_STATE_LEVEL    0
#define MD_STATE_MENU     1
#define MD_STATE_DEAD     2
#define MD_STATE_NONLEVEL 3

unsigned char md_game_state(void)
{
    if (menuactive) return MD_STATE_MENU;
    if (gamestate != GS_LEVEL || demoplayback) return MD_STATE_NONLEVEL;
    if (players[consoleplayer].playerstate == PST_DEAD) return MD_STATE_DEAD;
    return MD_STATE_LEVEL;
}
