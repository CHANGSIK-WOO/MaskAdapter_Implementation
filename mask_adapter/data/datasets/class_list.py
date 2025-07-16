# path
ade20k_path = r"C:\Users\woo chang sik\PythonProject\MaskAdapter_Implementation\mask_adapter\data\datasets\ade20k_150_with_prompt_eng.txt"
coco_path = r"C:\Users\woo chang sik\PythonProject\MaskAdapter_Implementation\mask_adapter\data\datasets\coco_stuff_with_prompt_eng.txt"

# open file and parsing
def parse_class_file(path):
    class_dict = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if ":" in line:
                class_id, class_names = line.split(":", 1)
                class_id = int(class_id.strip())
                names = set(name.strip().lower() for name in class_names.split(","))
                class_dict[class_id] = names
    return class_dict

ade_classes = parse_class_file(ade20k_path)
coco_classes = parse_class_file(coco_path)

# common and only ids
common_ids_tuple = []
ade_common_ids = []
ade_only_ids = []

for ade_id, ade_names in ade_classes.items():
    is_common = False
    for coco_id, coco_names in coco_classes.items():
        if ade_names & coco_names:
            common_ids_tuple.append((ade_id, coco_id))
            ade_common_ids.append(ade_id)
            is_common = True
            break
    if not is_common:
        ade_only_ids.append(ade_id)

print(common_ids_tuple)
print("\n")
print(ade_only_ids)
