#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
int main(int argc, char **argv) {
    int port = atoi(argv[1]), n = atoi(argv[2]);
    int s = socket(AF_INET, SOCK_STREAM, 0), one = 1;
    setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in a = {0}; a.sin_family = AF_INET; a.sin_port = htons(port); a.sin_addr.s_addr = INADDR_ANY;
    if (bind(s, (void *)&a, sizeof a) || listen(s, 1)) { perror("bind"); return 1; }
    int c = accept(s, 0, 0); setsockopt(c, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    char *b = malloc(n);
    for (;;) { int got = 0; while (got < n) { int r = read(c, b + got, n - got); if (r <= 0) return 0; got += r; }
               int sent = 0; while (sent < n) sent += write(c, b + sent, n - sent); }
}
