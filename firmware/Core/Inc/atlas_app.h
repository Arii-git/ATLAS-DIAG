/* ATLAS-DIAG application layer (ported from the earlier F446RE LCD build).
 * Kept outside main.c so CubeMX regeneration never touches it.
 * main.c only calls App_Init() once and App_Loop() forever. */
#ifndef ATLAS_APP_H
#define ATLAS_APP_H

void App_Init(void);
void App_Loop(void);

#endif /* ATLAS_APP_H */
