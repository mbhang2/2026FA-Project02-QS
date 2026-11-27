def kernel(event, book, state, orders):
    # The baseline kernel: it never trades. Every campaign of native kernels starts from it.
    # state[0] counts the events seen, to show how memory persists between calls.
    state[0] += 1.0
    return 0
