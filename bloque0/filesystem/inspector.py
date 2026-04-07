import os
import stat
import pwd
import grp
import sys
from datetime import datetime

def formato_permiso(modo):
    return stat.filemode(modo)

def tipo_archivo(modo):
    if stat.S_ISREG(modo):
        return "archivo regular"
    elif stat.S_ISDIR(modo):
        return "directorio"
    elif stat.S_ISLNK(modo):
        return "enlace simbólico"
    else:
        return "otro"

def main():
    if len(sys.argv) < 2:
        print("Uso: python inspector.py <ruta>")
        return

    ruta = sys.argv[1]

    try:
        info = os.lstat(ruta)

        print(f"Archivo: {ruta}")
        print(f"Tipo: {tipo_archivo(info.st_mode)}")
        print(f"Tamaño: {info.st_size} bytes")
        print(f"Permisos: {formato_permiso(info.st_mode)} ({oct(info.st_mode)[-3:]})")

        usuario = pwd.getpwuid(info.st_uid).pw_name
        grupo = grp.getgrgid(info.st_gid).gr_name

        print(f"Propietario: {usuario} (uid: {info.st_uid})")
        print(f"Grupo: {grupo} (gid: {info.st_gid})")

        print(f"Inodo: {info.st_ino}")
        print(f"Enlaces duros: {info.st_nlink}")

        print("Última modificación:", datetime.fromtimestamp(info.st_mtime))

        if os.path.islink(ruta):
            destino = os.readlink(ruta)
            print(f"→ Apunta a: {destino}")

        if os.path.isdir(ruta):
            cantidad = len(os.listdir(ruta))
            print(f"Contenido: {cantidad} elementos")

    except FileNotFoundError:
        print("El archivo no existe")
    except PermissionError:
        print("Sin permisos para acceder")

if __name__ == "__main__":
    main()