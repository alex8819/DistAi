// Linkato negli eseguibili: stdout/stderr senza buffer, così l'app riceve i log in tempo reale
#include <stdio.h>
__attribute__((constructor)) static void distai_unbuffer(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);
}
