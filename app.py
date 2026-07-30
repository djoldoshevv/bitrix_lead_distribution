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

@app.route('/distribute', methods=['POST'])
def distribute_lead():
    """
    Эндпоинт для распределения лида по алгоритму строгого Round-Robin.
    """
    try:
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
                # Ограничиваем запрос рабочего дня 3 секундами
                timeman_batch = call_bitrix_api("batch", {"halt": 0, "cmd": timeman_cmd}, timeout=3.0) or {}
                timeman_data = timeman_batch.get("result", {})
            except Exception as tm_err:
                # В случае зависания API Битрикса считаем всех активными, чтобы не ломать распределение
                print(f"[WARNING] Ошибка получения статуса рабочего дня (таймаут): {tm_err}")
                timeman_data = {}
            
            for m in managers:
                tm_status = timeman_data.get(f"timeman_{m['id']}")
                status_val = tm_status.get("STATUS") if isinstance(tm_status, dict) else None
                print(f"[DEBUG] TimeMan {m['name']} (ID {m['id']}): status={status_val}, raw={tm_status}")
                # Если рабочий день открыт (STATUS == 'OPENED')
                if tm_status and tm_status.get("STATUS") == "OPENED":
                    # Проверяем, не зависла ли смена (открыта более 16 часов назад)
                    start_str = tm_status.get("TIME_START")
                    if start_str:
                        try:
                            start_dt = datetime.fromisoformat(start_str)
                            now_dt = datetime.now(timezone.utc)
                            diff_hours = (now_dt - start_dt).total_seconds() / 3600.0
                            if diff_hours > 16:
                                print(f"[DEBUG] Смена {m['name']} (ID {m['id']}) открыта {diff_hours:.1f}ч назад -> ПРОПУСКАЕМ")
                                continue
                            else:
                                print(f"[DEBUG] Смена {m['name']} (ID {m['id']}) открыта {diff_hours:.1f}ч назад -> АКТИВЕН")
                        except Exception as time_err:
                            print(f"[DEBUG] Ошибка парсинга времени для {m['name']}: {time_err}")
                    working_pool.append(m)
                # Если сведений нет (ошибка/модуль отключен у юзера), разрешаем распределение (fallback)
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
        else:
            # Если история пуста (вытеснена лидами других отделов),
            # используем остаток от деления ID лида для псевдослучайного распределения
            try:
                start_index = int(lead_id) % len(managers)
            except ValueError:
                start_index = 0
                
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

@app.route('/health', methods=['GET'])
def health_check():
    return "OK", 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
