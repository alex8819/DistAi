import sys
B, E = "<[begin▁of▁sentence]>", "<[end▁of▁sentence]>"
def prompt(user):
    return (f"{B}system\nYou are an AI programming assistant, utilizing the Seed-Coder model, developed by ByteDance Seed, and you only answer questions related to computer science.\n\n{E}"
            f"{B}user\n{user}{E}{B}assistant\n")
if __name__ == "__main__": sys.stdout.write(prompt(sys.argv[1]))
