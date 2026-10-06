// Exercise the exact balena inspect template with Go's strict map lookups.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"text/template"
)

func main() {
	var data map[string]interface{}
	if err := json.NewDecoder(os.Stdin).Decode(&data); err != nil {
		panic(err)
	}
	tmpl, err := template.New("inspect").Option("missingkey=error").Funcs(template.FuncMap{
		"json": func(value interface{}) (string, error) {
			encoded, err := json.Marshal(value)
			return string(encoded), err
		},
	}).Parse(os.Args[1])
	if err == nil {
		err = tmpl.Execute(os.Stdout, data)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
