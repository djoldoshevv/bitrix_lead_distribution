from flask import Flask, request, jsonify
import json
import urllib.request
import urllib.error
import os
from datetime import datetime, timezone

app = Flask(__name__)

# === КОНФИГУРАЦИЯ ===
BITRIX_WEBHOOK_URL = os.environ.get("BITRIX_WEBHOOK_URL", "https://aquaman.bitrix24.ru/rest/12/cm6r30bba9i95p68/")
OPERATORS_DEPARTMENT_ID = int(os.environ.get("OPERATORS_DEPARTMENT_ID", 56))
FALLBACK_MANAGER_IDS = os.environ.get("FALLBACK_MANAGER_IDS", "3948,11844,44402").split(",")

# Вкл/Выкл проверку рабочего дня
CHECK_WORKDAY = True  

# Вкл/Выкл скрипт распределения (False = скрипт отключен, лиды не переназначаются)
ENABLED = True

def call_bitrix_api(method, params, timeout=8):
    """
    Выполняет POST-запрос к API Битрикс24.
    """
    url = BITRIX_WEBHOOK_URL + method
    req_data = json.dumps(params).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=req_data,
        headers={'Content-Type': 'application/json'}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            if 'error' in res_data:
                raise Exception(f"Bitrix24 Error: {res_data.get('error')} - {res_data.get('error_description')}")
            return res_data.get('result')
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode('utf-8')
            res_data = json.loads(err_body)
            err_msg = res_data.get('error_description') or res_data.get('error') or "Неизвестная ошибка REST API"
            raise Exception(f"Bitrix24 REST API returned error: {err_msg}")
        except Exception as inner_e:
            if "Bitrix24 REST API returned error" in str(inner_e):
                raise inner_e
            raise Exception(f"HTTP Error {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise Exception(f"Network error: {e.reason}")

def find_lead_id_recursively(data):
    """
    Автоматически ищет ID лида/сущности в структуре входящего JSON.
    """
    if not isinstance(data, (dict, list)):
        return None
        
    if isinstance(data, dict):
        for k, v in data.items():
            k_lower = str(k).lower()
            if k_lower in ("lead_id", "leadid", "bitrixentityid", "bitrix_entity_id", "entity_id", "entityid"):
                return v
            if "fields][id" in k_lower:
                return v
                
        for k, v in data.items():
            if str(k).upper() == "FIELDS" and isinstance(v, dict) and "ID" in v:
                return v["ID"]
            
            res = find_lead_id_recursively(v)
            if res:
                return res
                
    elif isinstance(data, list):
        for item in data:
            res = find_lead_id_recursively(item)
            if res:
                return res
                
    return None

def distribute_lead_logic():
    """
    Основная логика распределения лидов по Round-Robin.
    """
    try:
        if not ENABLED:
            return jsonify({"status": "disabled", "message": "Распределение временно отключено"}), 200
            
        data = request.get_json(silent=True) or {}
        if not data and request.form:
            data = request.form.to_dict()
            
        lead_id = find_lead_id_recursively(data)
        
        if not lead_id:
            return jsonify({
                "status": "error", 
                "message": "ID лида не найден во входящих данных"
            }), 400
            
        lead_id = str(lead_id).strip()
        print(f"[LOG] Начинаем распределение для Лида ID: {lead_id}")
            
        # 1. Получаем операторов и историю за один пакетный запрос
        initial_cmd = {
            "operators": f"user.get?ACTIVE=true&UF_DEPARTMENT={OPERATORS_DEPARTMENT_ID}",
            "recent_leads": "crm.lead.list?order[DATE_CREATE]=DESC&select[0]=ID&select[1]=ASSIGNED_BY_ID&limit=50"
        }
        
        initial_batch = call_bitrix_api("batch", {"halt": 0, "cmd": initial_cmd}, timeout=3.5) or {}
        batch_results = initial_batch.get("result", {})
        
        operators = batch_results.get("operators", [])
        recent_leads = batch_results.get("recent_leads", [])
        
        if not operators:
            return jsonify({
                "status": "error", 
                "message": f"Не найдены активные сотрудники в департаменте {OPERATORS_DEPARTMENT_ID}"
            }), 500
            
        managers = []
        for op in operators:
            managers.append({
                "id": str(op.get("ID")),
                "name": f"{op.get('NAME', '')} {op.get('LAST_NAME', '')}".strip() or f"Operator #{op.get('ID')}"
            })
        managers.sort(key=lambda x: x['id'])
        print(f"[DEBUG] Менеджеры ({len(managers)}): {[(m['name'], m['id']) for m in managers]}")
        
        # 2. Проверяем рабочий день сотрудников
        working_pool = []
        if not CHECK_WORKDAY:
            working_pool = managers
        else:
            timeman_cmd = {}
            for m in managers:
                timeman_cmd[f"timeman_{m['id']}"] = f"timeman.status?USER_ID={m['id']}"
                
            try:
                timeman_batch = call_bitrix_api("batch", {"halt": 0, "cmd": timeman_cmd}, timeout=3.0) or {}
                timeman_data = timeman_batch.get("result", {})
            except Exception as tm_err:
                print(f"[WARNING] Ошибка получения статуса рабочего дня (таймаут): {tm_err}")
                timeman_data = {}
            
            for m in managers:
                tm_status = timeman_data.get(f"timeman_{m['id']}")
                status_val = tm_status.get("STATUS") if isinstance(tm_status, dict) else None
                print(f"[DEBUG] TimeMan {m['name']} (ID {m['id']}): status={status_val}")
                if tm_status and tm_status.get("STATUS") == "OPENED":
                    print(f"[DEBUG] {m['name']} (ID {m['id']}): OPENED -> в пуле")
                    working_pool.append(m)
                elif not tm_status or "error" in tm_status:
                    print(f"[DEBUG] {m['name']} (ID {m['id']}): нет данных timeman -> добавляем в пул (fallback)")
                    working_pool.append(m)
                else:
                    print(f"[DEBUG] {m['name']} (ID {m['id']}): статус {status_val} -> НЕ в пуле")
                    
        if not working_pool:
            print(f"[DEBUG] Рабочий пул пуст! Используем fallback менеджеров: {FALLBACK_MANAGER_IDS}")
            working_pool = [m for m in managers if m['id'] in FALLBACK_MANAGER_IDS]
            if not working_pool:
                working_pool = managers
        print(f"[DEBUG] Рабочий пул ({len(working_pool)}): {[(m['name'], m['id']) for m in working_pool]}")
                
        # 3. Находим ID менеджера, который получил САМЫЙ ПОСЛЕДНИЙ лид
        last_assigned_manager_id = None
        if recent_leads:
            print(f"[DEBUG] Последние 5 лидов в истории: {[(str(l.get('ID')), str(l.get('ASSIGNED_BY_ID'))) for l in recent_leads[:5]]}")
            for l in recent_leads:
                if str(l.get("ID")) != str(lead_id):
                    assigned_id = str(l.get("ASSIGNED_BY_ID"))
                    if any(m['id'] == assigned_id for m in managers):
                        last_assigned_manager_id = assigned_id
                        break
        else:
            print(f"[DEBUG] История лидов ПУСТА!")
                        
        # 4. Алгоритм строгого циклического распределения (Round-Robin):
        start_index = 0
        if last_assigned_manager_id:
            for idx, m in enumerate(managers):
                if m['id'] == last_assigned_manager_id:
                    start_index = (idx + 1) % len(managers)
                    break
            print(f"[DEBUG] Последний назначенный: ID {last_assigned_manager_id} -> start_index={start_index}")
        else:
            try:
                start_index = int(lead_id) % len(managers)
            except ValueError:
                start_index = 0
            print(f"[DEBUG] История пуста для нашего отдела -> start_index={start_index} (lead_id % {len(managers)})")
                
        selected_manager = None
        for i in range(len(managers)):
            check_idx = (start_index + i) % len(managers)
            candidate = managers[check_idx]
            if any(w['id'] == candidate['id'] for w in working_pool):
                selected_manager = candidate
                break
                
        if not selected_manager:
            selected_manager = working_pool[0]
            
        print(f"[LOG] Лид {lead_id} -> назначаем на {selected_manager['name']} (ID {selected_manager['id']})")
        
        # 5. Назначаем ответственного менеджера в Битрикс24
        call_bitrix_api("crm.lead.update", {
            "id": lead_id,
            "fields": {
                "ASSIGNED_BY_ID": int(selected_manager['id'])
            }
        }, timeout=3.0)
        print(f"[LOG] Лид {lead_id} успешно назначен на {selected_manager['name']} (ID {selected_manager['id']})")
        
        return jsonify({
            "status": "success",
            "assigned_to": selected_manager['name'],
            "manager_id": selected_manager['id'],
            "last_assigned_manager_id": last_assigned_manager_id
        }), 200
        
    except Exception as e:
        print(f"[ERROR] Ошибка выполнения: {str(e)}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

def close_shifts_logic():
    """
    Закрывает все открытые смены менеджеров отдела.
    """
    try:
        operators = call_bitrix_api("user.get", {"ACTIVE": True, "UF_DEPARTMENT": OPERATORS_DEPARTMENT_ID}, timeout=5) or []
        
        if not operators:
            return jsonify({"status": "error", "message": "Нет операторов"}), 500
        
        timeman_cmd = {}
        for op in operators:
            op_id = str(op.get("ID"))
            timeman_cmd[f"tm_{op_id}"] = f"timeman.status?USER_ID={op_id}"
        
        tm_batch = call_bitrix_api("batch", {"halt": 0, "cmd": timeman_cmd}, timeout=5) or {}
        tm_data = tm_batch.get("result", {})
        
        closed = []
        errors = []
        
        for op in operators:
            op_id = str(op.get("ID"))
            op_name = f"{op.get('NAME', '')} {op.get('LAST_NAME', '')}".strip()
            tm = tm_data.get(f"tm_{op_id}")
            status = tm.get("STATUS") if isinstance(tm, dict) else None
            
            if status == "OPENED":
                print(f"[CRON] Закрываем смену: {op_name} (ID {op_id})")
                try:
                    call_bitrix_api("timeman.close", {"USER_ID": int(op_id)}, timeout=5)
                    closed.append(f"{op_name} (ID {op_id})")
                    print(f"[CRON] Смена {op_name} успешно закрыта")
                except Exception as close_err:
                    errors.append(f"{op_name}: {str(close_err)}")
                    print(f"[CRON] Ошибка закрытия смены {op_name}: {close_err}")
            else:
                print(f"[CRON] {op_name} (ID {op_id}): статус {status} — пропускаем")
        
        return jsonify({
            "status": "success",
            "closed": closed,
            "errors": errors,
            "message": f"Закрыто смен: {len(closed)}, ошибок: {len(errors)}"
        }), 200
        
    except Exception as e:
        print(f"[CRON ERROR] {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/', methods=['GET', 'POST'])
@app.route('/api/index', methods=['GET', 'POST'])
@app.route('/<path:subpath>', methods=['GET', 'POST'])
@app.route('/api/index/<path:subpath>', methods=['GET', 'POST'])
def universal_router(subpath=''):
    """
    Универсальный роутер для обработки всех путей Vercel.
    """
    full_str = f"{request.path} {request.url} {request.headers.get('x-forwarded-uri', '')} {subpath}".lower()
    
    if 'close-shifts' in full_str or request.args.get('action') == 'close-shifts':
        return close_shifts_logic()
        
    if 'health' in full_str:
        return jsonify({"status": "OK", "service": "Bitrix Lead Distribution"}), 200
        
    if request.method == 'GET':
        return jsonify({
            "status": "OK", 
            "service": "Bitrix Lead Distribution Service",
            "endpoints": ["/distribute (POST)", "/close-shifts (GET)", "/health (GET)"]
        }), 200
        
    return distribute_lead_logic()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
