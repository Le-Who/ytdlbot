import base64
import pickle

print("===========================================")
print("Инструмент создания сессии для Instagram")
print("===========================================\n")
print("Откройте Instagram в браузере на ПК, нажмите F12 (Инструменты разработчика)")
print("Перейдите во вкладку Application (Приложение) -> Cookies -> https://www.instagram.com\n")

sessionid = input("1. Введите значение куки 'sessionid': ").strip()
csrftoken = input("2. Введите значение куки 'csrftoken': ").strip()
ds_user_id = input("3. Введите значение куки 'ds_user_id': ").strip()
mid = input("4. Введите значение куки 'mid' (можно пропустить, нажмите Enter): ").strip()
ig_did = input("5. Введите значение куки 'ig_did' (можно пропустить, нажмите Enter): ").strip()

if not sessionid or not csrftoken or not ds_user_id:
    print("\n[ОШИБКА] sessionid, csrftoken и ds_user_id обязательны для обхода защиты!")
    exit(1)

cookies = {
    "sessionid": sessionid,
    "csrftoken": csrftoken,
    "ds_user_id": ds_user_id,
}
if mid:
    cookies["mid"] = mid
if ig_did:
    cookies["ig_did"] = ig_did

b64_string = base64.b64encode(pickle.dumps(cookies)).decode()

print("\n===========================================")
print("Готово! Ваш IG_SESSION_B64:")
print("===========================================\n")
print(b64_string)
print("\nСкопируйте эту мощную строку целиком и вставьте в .env файл!")
