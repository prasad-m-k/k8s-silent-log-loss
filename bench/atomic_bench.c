/* atomic_bench.c - cost of the per-line sequence increment.
 * Build: gcc -O2 -pthread atomic_bench.c -o atomic_bench
 * Run:   ./atomic_bench [threads]
 * Reports ns per increment for a relaxed atomic fetch-add on one shared
 * counter, the operation a logging wrapper runs once per line. */
#include <stdatomic.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

static _Atomic unsigned long long seq = 0;
static const long ITERS = 50000000;

static void *worker(void *arg) {
    (void)arg;
    for (long i = 0; i < ITERS; i++)
        atomic_fetch_add_explicit(&seq, 1, memory_order_relaxed);
    return NULL;
}

static double now(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
    int threads = argc > 1 ? atoi(argv[1]) : 1;
    pthread_t t[64];
    double best = 1e9;
    for (int run = 0; run < 5; run++) {
        atomic_store(&seq, 0);
        double t0 = now();
        for (int i = 0; i < threads; i++) pthread_create(&t[i], NULL, worker, NULL);
        for (int i = 0; i < threads; i++) pthread_join(t[i], NULL);
        double ns = (now() - t0) * 1e9 / (double)(ITERS * threads);
        if (ns < best) best = ns;
        printf("run %d: %.2f ns/increment (final=%llu)\n", run, ns, (unsigned long long)seq);
    }
    printf("threads=%d best=%.2f ns/increment\n", threads, best);
    return 0;
}
