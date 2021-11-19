# Copyright 2021 Good Chemistry Company.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Functions helping with quantum circuit format conversion between abstract
format and ionq format.

In order to produce an equivalent circuit for the target backend, it is
necessary to account for:
- how the gate names differ between the source backend to the target backend.
- how the order and conventions for some of the inputs to the gate operations
    may also differ.
"""


def get_ionq_gates():
    """Map gate name of the abstract format to the equivalent gate name used in
    the json IonQ format. For more information:
    - https://dewdrop.ionq.co/
    - https://docs.ionq.co
    """

    GATE_JSON_IONQ = dict()
    for name in {"H", "X", "Y", "Z", "S", "T", "RX", "RY", "RZ", "CNOT"}:
        GATE_JSON_IONQ[name] = name.lower()
    return GATE_JSON_IONQ


def translate_json_ionq(source_circuit):
    """Take in an abstract circuit, return a dictionary following the IonQ JSON
    format as described below.
    https://dewdrop.ionq.co/#json-specification

    Args:
        source_circuit: quantum circuit in the abstract format.

    Returns:
        dict: representation of the quantum circuit following the IonQ JSON
            format.
    """

    GATE_JSON_IONQ = get_ionq_gates()

    json_gates = []
    for gate in source_circuit._gates:
        if gate.name in {"H", "X", "Y", "Z", "S", "T"}:
            json_gates.append({'gate': GATE_JSON_IONQ[gate.name], 'target': gate.target})
        elif gate.name in {"RX", "RY", "RZ"}:
            json_gates.append({'gate': GATE_JSON_IONQ[gate.name], 'target': gate.target, 'rotation': gate.parameter})
        elif gate.name in {"CNOT"}:
            json_gates.append({'gate': GATE_JSON_IONQ[gate.name], 'target': gate.target, 'control': gate.control})
        else:
            raise ValueError(f"Gate '{gate.name}' not supported with JSON IonQ translation")

    json_ionq_circ = {"qubits": source_circuit.width, 'circuit': json_gates}
    return json_ionq_circ