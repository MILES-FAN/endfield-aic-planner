local data = {
{
    time = 8,
    mode = "Power",
    ingredients = { { item = "Originium Ore", count = 1 } },
    products = { { item = "Power", count = 50 } },
},
{
    time = 40,
    mode = "Power",
    ingredients = { { item = "LC Valley Battery", count = 1 } },
    products = { { item = "Power", count = 220 } },
},
{
    time = 40,
    mode = "Power",
    ingredients = { { item = "SC Valley Battery", count = 1 } },
    products = { { item = "Power", count = 420 } },
},
{
    time = 40,
    mode = "Power",
    ingredients = { { item = "HC Valley Battery", count = 1 } },
    products = { { item = "Power", count = 1100 } },
},
{
    time = 40,
    mode = "Power",
    ingredients = { { item = "LC Wuling Battery", count = 1 } },
    products = { { item = "Power", count = 1600 } },
},
}

for _, v in ipairs(data) do
    v.facility = "Thermal Bank"
end

return data