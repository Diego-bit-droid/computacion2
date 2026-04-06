import argparse
import sys


def buscar_en_archivo(file, patron, args, nombre_archivo=None):
    coincidencias = 0

    for i, linea in enumerate(file, start=1):
        texto = linea.rstrip("\n")

        # Manejo de ignore case
        if args.ignore_case:
            match = patron.lower() in texto.lower()
        else:
            match = patron in texto

        # Invertir búsqueda
        if args.invert:
            match = not match

        if match:
            coincidencias += 1

            if not args.count:
                salida = ""

                # Nombre archivo si hay múltiples
                if nombre_archivo:
                    salida += f"{nombre_archivo}:"

                # Número de línea
                if args.line_number:
                    salida += f"{i}:"

                salida += texto
                print(salida)

    return coincidencias


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("patron")
    parser.add_argument("archivos", nargs="*")

    parser.add_argument("-i", "--ignore-case", action="store_true")
    parser.add_argument("-n", "--line-number", action="store_true")
    parser.add_argument("-c", "--count", action="store_true")
    parser.add_argument("-v", "--invert", action="store_true")

    args = parser.parse_args()

    total = 0

    # 📌 Caso 1: stdin
    if not args.archivos:
        if not sys.stdin.isatty():
            total = buscar_en_archivo(sys.stdin, args.patron, args)
            if args.count:
                print(f"{total} coincidencias")
        else:
            print("Error: no hay archivos ni entrada por stdin")
        return

    # 📌 Caso 2: archivos
    multiples = len(args.archivos) > 1

    for nombre in args.archivos:
        try:
            with open(nombre, "r") as f:
                count = buscar_en_archivo(
                    f,
                    args.patron,
                    args,
                    nombre_archivo=nombre if multiples else None
                )

                total += count

                if args.count:
                    print(f"{nombre}: {count} coincidencias")

        except FileNotFoundError:
            print(f"No se pudo abrir {nombre}")

    if args.count and multiples:
        print(f"Total: {total} coincidencias")


if __name__ == "__main__":
    main()