import json


class Catalog:
    def __init__(self, path):
        vehicles = json.loads(path.read_text(encoding='utf-8'))['vehicles']
        self.vehicles = {v['vehicle_id']: v for v in vehicles}

    def pair(self, vehicle_id, session_id):
        vehicle = self.vehicles.get(vehicle_id)
        if vehicle:
            for session in vehicle['sessions']:
                if session['session_id'] == session_id:
                    return vehicle, session
        raise ValueError('Проверка не принадлежит выбранной машине.')

    def public(self):
        return [{**{k: v.get(k) for k in ('vehicle_id', 'garage_no', 'make', 'model', 'engine_model')},
                 'sessions': [{k: s[k] for k in ('session_id', 'checked_at')} for s in v['sessions']]}
                for v in self.vehicles.values()]
